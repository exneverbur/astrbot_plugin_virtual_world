"""场景记忆引擎（设计文档 4.13 节）。

记忆与「地点 + 人」强相关，因此召回用 SQL 过滤 + 权重打分即可，不需要向量检索：
- 召回空间天然很小（同一会话、同一节点、同一人格的记忆通常几十条）；
- 过滤维度是结构化的（会话/人格/节点/用户），SQL 比向量更精确；
- 省掉 embedding provider 依赖和每次召回的额外 API 开销。
如果将来要做「同义语义召回」，可以在 recall 后面挂一个可选的向量重排，
当前接口（recall 返回打分后的列表）已经预留了这个位置。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .db import Database
from .models import WorldConfig
from .time_ref import normalize_relative_dates

from . import memory_supersede

logger = logging.getLogger(__name__)

SCENE = "scene"
RELATION = "relation"
EVENT = "event"
INNER = "inner"
INTERACTION = "interaction"

SCOPE_GROUP = "group"
SCOPE_PERSONA = "persona"
SCOPE_GROUP_PERSONA = "group_persona"
SCOPE_GLOBAL = "global"
SCOPE_NODE = "node"

ACTION_SCENE_PREFIX = "在这里"
"""动作场景记忆的固定前缀（引擎 ``_remember_action`` 写 ``在这里{动作名}``）。

这类条目是"她此刻站在哪儿做了什么"的流水账，不是"值得记住的事"：噪声大、每个
tick 一条、还会抢走主回复那 5 个召回名额。所以主回复召回把它们排除
（``recall(exclude_action_scenes=True)``），自主 tick 的召回仍保留（她得知道
"我刚才一直在这儿发呆"）。
"""

_NEGATION_WORDS = ("不", "没", "别", "讨厌", "不想", "不再")

# 情绪强度词典：写进记忆时，"越激烈的经历记得越牢"。
# 值直接作为权重加成（0~0.25），所以强情绪的记忆更容易被召回。
_INTENSE_WORDS: dict[str, float] = {
    "生气": 0.22, "愤怒": 0.25, "吵架": 0.24, "讨厌": 0.18, "难过": 0.20,
    "委屈": 0.22, "心疼": 0.20, "想哭": 0.24, "哭": 0.16, "害怕": 0.20,
    "担心": 0.16, "害怕失去": 0.25, "喜欢": 0.18, "爱": 0.22, "想念": 0.20,
    "开心": 0.16, "高兴": 0.14, "激动": 0.20, "兴奋": 0.20, "感动": 0.22,
    "惊喜": 0.20, "害羞": 0.16, "紧张": 0.16, "嫉妒": 0.22, "吃醋": 0.22,
    "重要": 0.12, "第一次": 0.12, "约定": 0.12, "承诺": 0.14, "对不起": 0.14,
    "谢谢": 0.10, "安慰": 0.14, "陪我": 0.14, "别走": 0.22, "秘密": 0.16,
}

def _one_line(value: Any, limit: int = 120) -> str:
    """压成一行（提示词里不能出现换行）。"""

    text = " ".join(str(value or "").split())
    limit = max(1, int(limit))
    return text if len(text) <= limit else text[:limit] + "…"


def emotional_weight(
    base: float,
    *,
    emotion: str = "",
    text: str = "",
    affect: float = 0.0,
    valence: float = 0.5,
) -> float:
    """按"这段记忆的情绪有多强"给权重加成。

    加成来源（取最大的一项 + 情绪两轴的一小部分，避免叠加爆炸）：
    - 显式情绪标签（动作/编辑器里填的）
    - 正文里出现的强情绪词
    - 当时的情绪两轴：心潮（有多激动）× 效价离中性有多远

    以前这里还查一张「mood 词 → 强度」的表；心情词改成由两轴派生之后，
    那张表就成了第二套映射（改词表就会悄悄失效），所以这里直接用数值算。
    """

    scored: list[float] = []
    if emotion:
        scored.append(max((_INTENSE_WORDS.get(word, 0.0) for word in _emotion_tokens(emotion)), default=0.12))
    if text:
        hits = [value for word, value in _INTENSE_WORDS.items() if word in text]
        if hits:
            scored.append(max(hits))
    bonus = max(scored) if scored else 0.0
    # 越激动、心情偏得越远，记得越牢（心潮高但心情平平，只是一时热闹）
    arousal = max(0.0, min(1.0, float(affect)))
    distance = abs(max(0.0, min(1.0, float(valence))) - 0.5) * 2.0
    bonus += arousal * (0.06 + 0.14 * distance)
    return max(0.0, min(1.0, float(base) + bonus))


def _emotion_tokens(emotion: str) -> list[str]:
    return [part.strip() for part in re.split(r"[/、,，\s]+", str(emotion or "")) if part.strip()]


@dataclass
class RecalledMemory:
    """召回结果。"""

    id: int
    content: str
    node_id: str
    memory_type: str
    emotion: str
    weight: float
    score: float
    related_users: list[str]
    created_at: float = 0.0
    """写入时间（unix 秒）。提示词里会带上日期，所以它必须跟着召回结果一起走。"""

    tier: str = "raw"
    """``raw`` = 原始片段；``gist`` = 睡眠整理压出来的要点。"""

    context: str = ""
    """当时的原话片段（折叠之后正文只剩要点，靠它保留"她当时是怎么说的"）。"""

    participants: list[str] = field(default_factory=list)
    """这件事有谁在场（含说话人；一条记忆可以有多个人）。"""

    status: str = "active"
    """``active`` = 现在还算数；``superseded`` = 被同一件事的新说法覆盖（旧说法）。

    覆盖不删行：她提到「以前 / 之前」时把 ``include_superseded=True`` 打开，旧条目
    仍然看得到，只是渲染时会带「（旧说法）」前缀，免得她把两条当成两件事。
    """

    superseded_by: str = ""
    """覆盖它的那条记忆 id（回看用）。"""

    def render(self, *, stamp: str = "") -> str:
        """渲染成提示词里的一行；``stamp`` 是日期标签，例如 ``09-11``。"""

        prefix = f"[{stamp}] " if stamp else ""
        suffix = f"（{self.emotion}）" if self.emotion else ""
        detail = ""
        if self.tier == "gist" and self.context:
            # 要点 + 当时那句原话：她记得住结论，也说得清细节
            detail = f"｜当时：{_one_line(self.context, 80)}"
        # "（旧说法）"放行首：这一行整体是"以前是这么说的"，不是"现在还是这样"
        old = "（旧说法）" if self.status == "superseded" else ""
        return f"- {old}{prefix}{self.content}{suffix}{detail}"


def _memory_key(text: str) -> str:
    """整理时判"是不是同一句话"：去空白、去标点、统一大小写。"""

    return "".join(ch for ch in str(text or "") if ch.isalnum()).lower()


def normalize_for_memory(text: str, now: float | None = None) -> str:
    """写进记忆前把相对时间换成绝对日期（原话留在句末）。

    「我明天要去体检」存进去，第二天读还是"明天"，她就会把已经做完的事当成还没做
    （用户实机事故）。所以**所有记忆 / 事实写入点**统一走这里，规则见
    :mod:`core.time_ref`：

    * 认得出相对时间词 → 换成 ``YYYY-MM-DD``，句末追加 ``（原话：…）`` 保溯源；
    * 认不出 / 没有相对词 → 原样返回（历史数据、纯叙述都不受影响）；
    * 幂等：已经归一过的文本再走一遍不会变（``_write_memory`` 与 ``remember``
      都会调，不能二次换算）。

    ``now`` 是"她认为的现在"（世界时钟），缺省用真实时钟；归一化本身绝不该
    让一条记忆写不进去，所以任何异常都退化成原文。
    """

    body = str(text or "")
    if not body.strip():
        return body
    try:
        stamp = float(now) if now is not None else time.time()
        moment = datetime.fromtimestamp(stamp).astimezone()
        rewritten, _pairs = normalize_relative_dates(body, moment)
        return rewritten or body
    except Exception:  # noqa: BLE001 - 时间换算坏了也不能丢记忆
        return body


_ENUM_TAIL = re.compile(
    r"[;\uff1b,\uff0c\u3001]\s*"
    r"(?:success|fail|failed|ok|pending|done|true|false|none|closed|active)\s*$",
    re.IGNORECASE,
)


def _strip_enum_tail(body: str) -> str:
    """⑥（自主流）：事件记忆写库时把 JSON 的枚举尾巴（「…；success」）带进正文了。

    提示词会原样读到它，所以写库前统一剥掉；只剥句末那一截，不动正文。
    """

    value = str(body or "")
    for _ in range(3):
        trimmed = _ENUM_TAIL.sub("", value).rstrip()
        if trimmed == value:
            break
        value = trimmed
    return value


class MemoryEngine:
    """记忆的创建、召回、衰减与用户控制。"""

    def __init__(
        self,
        db: Database,
        world: WorldConfig | None = None,
        *,
        sessions_of: Any = None,
        skip_session: Any = None,
        now: Any = None,
    ) -> None:
        self.db = db
        self.world = world
        # 「查的时候看哪些会话」的换算：记忆**按会话各存一份**（这样以后改组
        # 也不会把记忆挂在错的地方），但召回时把同组的会话一起当条件。
        self._sessions_of = sessions_of
        # 「这个会话要不要写记忆」的闸：临时会话（关系还没到熟悉）一律不写。
        # 挂成回调而不是在这里 import 引擎，是为了让 memory 模块不认识世界状态。
        self._skip_session = skip_session
        # 时钟：写记忆时要把"明天"换算成日期，用的是**她那边的现在**（世界时钟
        # 可能和真实时钟不同）。挂成回调，缺省退回 time.time()。
        self._now_provider = now

    def set_now(self, hook: Any) -> None:
        self._now_provider = hook

    def _now(self) -> float:
        hook = self._now_provider
        if callable(hook):
            try:
                return float(hook())
            except Exception:  # noqa: BLE001 - 时钟坏了按真实时间走
                pass
        return time.time()


    def set_skip_session(self, hook: Any) -> None:
        self._skip_session = hook

    def _skip(self, session_id: str) -> bool:
        if not callable(self._skip_session):
            return False
        try:
            return bool(self._skip_session(str(session_id or "")))
        except Exception:  # noqa: BLE001 - 闸坏了按"照常写"处理，不能因为判权限丢记忆
            return False

    def sessions(self, session_id: str) -> list[str]:
        """召回时要一起看的会话（会话组 = 组里所有会话）。"""

        text = str(session_id or "")
        if callable(self._sessions_of):
            try:
                items = [str(item) for item in (self._sessions_of(text) or []) if str(item)]
            except Exception:
                items = []
            if items:
                return items
        return [text] if text else []

    def set_world(self, world: WorldConfig) -> None:
        self.world = world

    # ---------------- 创建 ----------------

    def remember(
        self,
        *,
        session_id: str,
        persona_id: str,
        node_id: str,
        content: str,
        memory_type: str = SCENE,
        related_users: list[str] | None = None,
        emotion: str = "",
        weight: float = 0.5,
        scope: str | None = None,
        source: str = "runtime",
        conflict_policy: str | None = None,
        affect: float = 0.0,
        valence: float = 0.5,
        tier: str = "raw",
        context: str = "",
        participants: list[str] | None = None,
        keywords: list[str] | None = None,
        next_review_at: float = 0.0,
        pinned: bool = False,
    ) -> int | None:
        """写入一条记忆。返回记忆 id；因冲突被跳过时返回 None。

        ``weight`` 是基础权重，实际写入时会被「情绪强度」抬高：
        越激动的经历记得越牢（见 :func:`emotional_weight`）。
        """

        text = (content or "").strip()
        if not text:
            return None
        # 「我明天要去体检」不能按月存：明天会过期，第二天她就会把做完的事当成还没做。
        # 写库这一刻换成绝对日期，原话留在句末（见 ``core/time_ref``）。
        text = normalize_for_memory(text, now=self._now())
        text = _strip_enum_tail(text)
        if not text:
            return None
        if self._skip(session_id):
            # 临时会话（关系还没到熟悉）：一条都不写。她的记忆只属于"真的认识的人"，
            # 陌生人的临时私聊不该在她脑子里留下东西（已存在的关系不删，只是不新增）。
            return None
        if not scope:
            mode = self.world.memory_scope_mode if self.world else SCOPE_GROUP_PERSONA
            # 空 persona = 「无主」写入：默认桶一律落 SCOPE_GROUP，**不落 group_persona**。
            # group_persona 是"组内共享"的桶（同一会话组里谁都能看到），空身份写进去
            # 就等于把无主记忆和具名人格的记忆混在同一个桶里——以前谁都能读到别人的。
            # 调用方显式传的 scope（node / persona / global…）仍然按它的意思走。
            if not str(persona_id or "") and mode in (SCOPE_GROUP_PERSONA, SCOPE_PERSONA):
                mode = SCOPE_GROUP
            scope = mode
        policy = conflict_policy or (
            self.world.memory_conflict_policy if self.world else "newest"
        )
        related_users = [str(u) for u in (related_users or [])]
        weight = emotional_weight(
            weight,
            emotion=emotion,
            text=text,
            affect=affect,
            valence=valence,
        )

        # 跨节点去重（同 session、同人格、归一化正文全等）：同一句话她在书房说一遍、
        # 在厨房又说一遍，旧代码在两个节点各存一份（实测同句 9 份）。命中就合并到已有
        # 那一行上——权重取高、节点留非空的那个、人名/关键词并起来，不静默丢信息。
        #
        # 动作流水账（「在这里…」）**不走这两条合并/替换**：它自己有一套按 30 分钟窗口
        # 的合并（:meth:`remember_action`），10:00 和 10:31 各发一次呆是两次观察，
        # 不能因为字面相同就永远并成一条，也不能被 conflict_policy 删掉。
        is_action = str(text).startswith(ACTION_SCENE_PREFIX)
        if not is_action:
            merged_id = self._merge_duplicate(
                session_id=session_id,
                persona_id=persona_id,
                node_id=node_id,
                content=text,
                weight=weight,
                related_users=related_users,
                participants=participants,
                tier=tier,
                context=context,
            )
            if merged_id is not None:
                return merged_id

        conflict = (
            None
            if is_action
            else self._find_conflict(
                session_id=session_id,
                persona_id=persona_id,
                node_id=node_id,
                content=text,
            )
        )
        conflict_by = ""
        if conflict is not None:
            if policy == "skip_conflict":
                return None
            if policy == "highest_weight" and float(conflict["weight"]) > float(weight):
                return None
            # 以前这里是 `self.db.delete_memories(...)`：同一件事说了相反的话，就把旧的
            # 物理删掉。删了就再也想不起来，用户明确要「不物理删除、可回看」——所以改成
            # 作废：新条目落库后把旧条目标 superseded（superseded_by=新 id），行为上对
            # 召回完全一样（默认只查 active），但旧说法还在库里。
            conflict_by = str(conflict.get("id") or "")

        # 路 B：写入时实时判「同主题覆盖」——不等夜间整理。
        #
        # 用户实机翻车：她对主人说「我明天要去体检」，可昨天说过、今天下午已经做完，
        # 库里两条并存，她混着说。判定必须是纯函数（core/memory_supersede.py：同人同
        # 类型 + 文本相似 + 状态/实体冲突），这里只负责"先判、后写、再作废旧条目"：
        # 新条目的 id 要先落库才能写进 superseded_by，所以判定在 add 之前、应用在之后。
        # 失败一律不影响写入本身（记忆写不进去比旧说法没作废更糟）。
        pending_supersede: list[tuple[dict[str, Any], memory_supersede.Verdict]] = []
        if not is_action:
            try:
                pending_supersede = self._supersede_candidates(
                    session_id=session_id,
                    persona_id=persona_id,
                    node_id=node_id,
                    content=text,
                    memory_type=memory_type,
                    people=[
                        str(item)
                        for item in (participants or related_users or [])
                        if str(item)
                    ],
                )
            except Exception as exc:  # noqa: BLE001 - 判定失败不该挡写入
                logger.warning("记忆覆盖判定失败，按不覆盖处理：%s", exc)
                pending_supersede = []

        self._enforce_limit(
            session_id=session_id, persona_id=persona_id, reserve=1
        )
        new_id = self.db.add_memory(
            session_id=session_id,
            persona_id=persona_id,
            scope=scope,
            node_id=node_id,
            content=text,
            memory_type=memory_type,
            related_users=related_users,
            emotion=emotion,
            weight=max(0.0, min(1.0, float(weight))),
            source=source,
            tier=str(tier or "raw"),
            context=str(context or ""),
            participants=[str(item) for item in (participants or related_users or [])],
            keywords=[str(item) for item in (keywords or [])],
            next_review_at=float(next_review_at or 0.0),
            pinned=bool(pinned),
        )
        if new_id and conflict_by:
            # 冲突策略命中的旧条目：作废（不删行），事件里带上策略名方便回溯
            try:
                self.db.supersede_memory(
                    int(conflict_by),
                    by=int(new_id),
                    why=f"同一件事说了相反的话（冲突策略 {policy}）",
                    via="conflict",
                )
            except Exception as exc:  # noqa: BLE001 - 作废失败不该影响这次写入
                logger.warning("记忆冲突改写作废失败：%s", exc)
        if pending_supersede and new_id:
            self._apply_supersede(new_id, pending_supersede, session_id=session_id)
        return new_id

    def _supersede_candidates(
        self,
        *,
        session_id: str,
        persona_id: str,
        node_id: str,
        content: str,
        memory_type: str,
        people: list[str],
        now: float | None = None,
    ) -> list[tuple[dict[str, Any], memory_supersede.Verdict]]:
        """路 B 的"取候选 + 判定"：返回 ``[(旧行, 判定)]``，判定只可能是 supersede/relate。

        候选口径（用户定的规则）：同会话组、同人格、同 ``type``、14 天内、仍 ``active``，
        最多 :data:`memory_supersede.MAX_CANDIDATES` 条。跨人**绝不**覆盖：两边都写明了
        是谁（participants / related_users）且对不上，就直接跳过——甲体检完了不能作废
        乙"明天要去体检"。
        """

        stamp = float(now if now is not None else time.time())
        since = stamp - memory_supersede.CANDIDATE_DAYS * 86400.0
        rows = self.db.memory_supersede_candidates(
            session_ids=self.sessions(session_id),
            persona_id=str(persona_id or ""),
            memory_type=str(memory_type or ""),
            since=since,
            limit=memory_supersede.MAX_CANDIDATES,
        )
        mine = {str(item) for item in (people or []) if str(item)}
        judged: list[tuple[dict[str, Any], memory_supersede.Verdict]] = []
        for item in rows:
            theirs = {
                str(entry)
                for key in ("participants", "related_users")
                for entry in (item.get(key) or [])
                if str(entry)
            }
            if mine and theirs and not (mine & theirs):
                continue  # 跨人：两条说的是两个人的事，不许互相覆盖
            verdict = memory_supersede.judge(str(item.get("content") or ""), content)
            if verdict.supersede or verdict.relate:
                judged.append((item, verdict))
        return judged

    def _apply_supersede(
        self,
        new_id: int,
        judged: list[tuple[dict[str, Any], memory_supersede.Verdict]],
        *,
        session_id: str = "",
    ) -> None:
        """把判定结果落库：``supersede`` 改状态 + 记事件；``relate`` 只记一条关联事件。"""

        for item, verdict in judged:
            old_id = int(item.get("id") or 0)
            if old_id <= 0 or old_id == int(new_id):
                continue
            try:
                if verdict.supersede:
                    self.db.supersede_memory(
                        old_id,
                        by=int(new_id),
                        why=verdict.why,
                        via="rule",
                    )
                else:
                    # 同主题但没冲突：只留一条"这两条在说同一件事"的线索，
                    # 绝不覆盖（喜欢猫 / 喜欢狗就停在这里）。
                    self.db.add_event(
                        session_id=session_id,
                        event_type="memory_related",
                        detail={
                            "a": old_id,
                            "b": int(new_id),
                            "why": verdict.why,
                            "score": round(float(verdict.score), 3),
                        },
                    )
            except Exception as exc:  # noqa: BLE001 - 作废失败不该回滚新记忆
                logger.warning("记忆作废失败（old=%s new=%s）：%s", old_id, new_id, exc)

    def supersede(
        self,
        memory_id: int,
        *,
        by: object = "",
        why: str = "",
        when: float | None = None,
        via: str = "manual",
    ) -> bool:
        """把一条记忆标成"旧说法"（夜间整理 / 人工都用这个口子）。"""

        return self.db.supersede_memory(
            int(memory_id), by=by, why=why, when=when, via=via
        )

    def restore(self, memory_id: int, *, when: float | None = None) -> bool:
        """把一条被覆盖的记忆恢复成 active（误判时的人工出口）。"""

        return self.db.restore_memory(int(memory_id), when=when)

    def _find_conflict(
        self, *, session_id: str, persona_id: str, node_id: str, content: str
    ) -> dict[str, Any] | None:
        """找出与待写入内容冲突的旧记忆。"""

        candidates = self.db.query_memories(
            session_id=session_id, persona_id=persona_id, node_id=node_id, limit=200
        )
        normalized = _normalize(content)
        for item in candidates:
            other = _normalize(str(item.get("content", "")))
            if not other:
                continue
            if other == normalized:
                return item
            if _opposite(normalized, other):
                return item
        return None

    def _merge_duplicate(
        self,
        *,
        session_id: str,
        persona_id: str,
        node_id: str,
        content: str,
        weight: float,
        related_users: list[str] | None,
        participants: list[str] | None,
        tier: str,
        context: str,
    ) -> int | None:
        """同会话内按归一化正文找同一条记忆（跨节点），命中就合并并返回它的 id。

        合并规则（都是"只加不减"，不做 embedding、不联网、结果确定）：
        - 权重取两者较大者（不因为重复写得晚就把重要的那条压低）；
        - 节点：已有那条为空才补（"保留节点信息"——她第一次在哪儿说的算哪儿）；
        - 人名/参与者：取并集，谁都不会被合并掉；
        - 层次：raw + gist 相遇取 gist（要点比原话更耐用）；
        - 当时原话：已有为空才补。

        只看同 session 最近 200 条（与 ``_find_conflict`` 同一个窗口）：记忆表的
        "最近一段"才有重复可谈，全表扫会把热路径拖慢。
        """

        normalized = _normalize(content)
        if not normalized:
            return None
        candidates = self.db.query_memories(
            session_id=session_id, persona_id=persona_id, limit=200
        )
        match: dict[str, Any] | None = None
        for item in candidates:
            if _normalize(str(item.get("content", ""))) == normalized:
                match = item
                break
        if match is None:
            return None

        current_weight = float(match.get("weight") or 0.0)
        fields: dict[str, Any] = {}
        if float(weight) > current_weight + 1e-6:
            fields["weight"] = min(1.0, float(weight))
        if not str(match.get("node_id") or "") and str(node_id or ""):
            fields["node_id"] = str(node_id)
        users = _union_text(match.get("related_users") or [], related_users or [])
        if users != [str(item) for item in (match.get("related_users") or [])]:
            fields["related_users"] = users
        people = _union_text(match.get("participants") or [], participants or [])
        if people != [str(item) for item in (match.get("participants") or [])]:
            fields["participants"] = people
        if str(tier or "raw") == "gist" and str(match.get("tier") or "raw") != "gist":
            fields["tier"] = "gist"
        if not str(match.get("context") or "").strip() and str(context or "").strip():
            fields["context"] = str(context)[:600]
        if fields:
            self.db.update_memory(int(match["id"]), **fields)
        return int(match["id"])

    def _memory_limit(self) -> int:
        """每个会话（同人格）最多留多少条记忆；``memory.memory_limit``，默认 1000。"""

        config = self.world.memory if self.world is not None else None
        try:
            value = int(getattr(config, "memory_limit", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else 1000

    def _enforce_limit(
        self,
        *,
        session_id: str,
        persona_id: str,
        max_per_scope: int | None = None,
        reserve: int = 0,
    ) -> None:
        """容量闸：超了就按「重复条目 → 动作噪声 → 长期未召回」的顺序淘汰。

        以前是 ``sorted(weight)`` 升序删——权重低的先走，而低权重的正是"还没被召回
        过的新记忆"，于是越不常被想起来的越先被删，越删越只剩老几条（马太效应）。

        现在按阶段淘汰，且**7 天内被召回过的条目一律不删**（刚用过的东西不能因为
        容量闸就消失）、pinned 不删。上限可配置（``memory.memory_limit``）。

        ``reserve``：调用方马上还要插进来几条。``remember`` 在插入**之前**调这里，
        不带 reserve 的话表会稳稳停在"上限 + 1"。
        """

        limit = (
            max(1, int(max_per_scope))
            if max_per_scope is not None
            else self._memory_limit()
        )
        # 多取一截：既要看见"超了多少"，又不能把整张表拉进内存
        rows = self.db.query_memories(
            session_id=session_id, persona_id=persona_id, limit=limit * 2 + 200
        )
        if len(rows) <= limit:
            return
        config = self.world.memory if self.world is not None else None
        try:
            protect_days = float(getattr(config, "evict_protect_days", 7.0) or 7.0)
        except (TypeError, ValueError):
            protect_days = 7.0
        cutoff = time.time() - max(0.0, protect_days) * 86400.0

        def protected(item: dict[str, Any]) -> bool:
            if item.get("pinned"):
                return True
            last = float(item.get("last_recalled") or 0.0)
            return last > 0 and last >= cutoff

        duplicates = self._duplicate_ids(rows)

        def rank(item: dict[str, Any]) -> tuple[int, int, float, float]:
            last_seen = float(item.get("last_recalled") or 0.0) or float(
                item.get("created_at") or 0.0
            )
            return (
                0 if int(item["id"]) in duplicates else 1,
                0 if _is_action_scene(item) else 1,
                last_seen,
                float(item.get("weight") or 0.0),
            )

        deletable = [item for item in rows if not protected(item)]
        deletable.sort(key=rank)
        need = len(rows) - limit + max(0, int(reserve))
        victims = [int(item["id"]) for item in deletable[: max(0, need)]]
        if victims:
            self.db.delete_memories_by_ids(victims)

    @staticmethod
    def _duplicate_ids(rows: list[dict[str, Any]]) -> set[int]:
        """同一批行里"同一句话"的多余副本（每组留权重最高、最新的那条）。"""

        buckets: dict[str, list[dict[str, Any]]] = {}
        for item in rows:
            key = _memory_key(str(item.get("content", "")))
            if not key:
                continue
            buckets.setdefault(key, []).append(item)
        drop: set[int] = set()
        for items in buckets.values():
            if len(items) < 2:
                continue
            items.sort(
                key=lambda item: (
                    float(item.get("weight") or 0.0),
                    float(item.get("created_at") or 0.0),
                )
            )
            for item in items[:-1]:
                if item.get("pinned"):
                    continue
                drop.add(int(item["id"]))
        return drop

    # ---------------- 召回 ----------------

    def recall(
        self,
        *,
        session_id: str,
        persona_id: str,
        node_id: str = "",
        nodes: list[str] | None = None,
        keyword: str = "",
        memory_type: str = "",
        focus_user: str = "",
        mode: str | None = None,
        limit: int = 5,
        now: float | None = None,
        fallback: bool = True,
        exclude_action_scenes: bool = False,
        include_superseded: bool = False,
    ) -> list[RecalledMemory]:
        """召回记忆。返回按分数降序的结果。

        ``node_id`` / ``nodes``：按地点过滤（``nodes`` 用于"回想某个区域里的所有地点"）。
        ``keyword``：主题关键词，命中越多的排越前（是加成，不是硬过滤）。
        ``exclude_action_scenes``：排除「在这里…」这类动作流水账。**默认关**，
        只有主回复那条路径传 True（见 :data:`ACTION_SCENE_PREFIX`）；自主 tick 的
        召回照旧看得见自己的动作，行为逐字节不变。
        ``include_superseded``：**默认关**——被同一件事的新说法覆盖掉的旧条目
        （``status='superseded'``）不进召回，否则她会把"明天要去体检"和"体检完了"
        一起想起来，混着说。她说「以前 / 之前 / 从前」时才由调用方打开，旧条目
        渲染时带「（旧说法）」前缀。
        """

        mode = mode or (self.world.memory_scope_mode if self.world else SCOPE_GROUP_PERSONA)
        now = now if now is not None else time.time()
        wanted_sessions = self.sessions(session_id)
        wanted_nodes = [str(item) for item in (nodes or []) if str(item)]
        if node_id and node_id not in wanted_nodes:
            wanted_nodes.append(node_id)
        candidates = self._query(
            session_id,
            persona_id,
            mode,
            node_id,
            nodes=wanted_nodes,
            memory_type=memory_type,
            session_ids=wanted_sessions,
            include_superseded=include_superseded,
        )
        # 降级策略只用于「人格未知」（persona_id 为空）的旧数据兼容：
        # 此时把本会话的所有记忆拉进来，但绝不跨会话取 group 级记忆，避免隐私泄露。
        if not candidates and fallback and not persona_id:
            candidates = self._query(
                session_id,
                persona_id,
                SCOPE_GROUP,
                node_id,
                nodes=wanted_nodes,
                memory_type=memory_type,
                session_ids=wanted_sessions,
                include_superseded=include_superseded,
            )

        if exclude_action_scenes:
            # 只排「在这里…」那种动作流水账；真记忆（谈话、事件、关系）一条不动。
            candidates = [item for item in candidates if not _is_action_scene(item)]

        scored: list[tuple[float, dict[str, Any]]] = []
        for item in candidates:
            score = self._score(item, focus_user=focus_user, node_id=node_id, now=now)
            if keyword:
                score *= self._keyword_bonus(content=item.get("content", ""), keyword=keyword)
            if score <= 0:
                continue
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)

        if keyword:
            # 主题词一个都没命中的，只在没别的东西可 recall 时才拿出来垫底
            hits = [pair for pair in scored if self._keyword_hits(pair[1].get("content", ""), keyword)]
            if hits:
                scored = hits
        results = [self._to_recalled(score, item) for score, item in scored[:limit]]
        if results:
            self.db.mark_recalled([item.id for item in results], when=now)
        return results

    def _query(
        self,
        session_id: str,
        persona_id: str,
        mode: str,
        node_id: str = "",
        *,
        nodes: list[str] | None = None,
        memory_type: str = "",
        session_ids: list[str] | None = None,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        """按作用域模式取候选记忆。

        语义（写死在测试里，改动会在 tests/test_memory.py 里暴露）：
        - group：本会话的全部记忆 + 全局记忆；
        - persona：该人格在所有会话的记忆 + 全局记忆；
        - group_persona：本会话本身的 + 该人格的 + 全局（默认，最贴合「同一个她在不同群里共享人格记忆」）；
        - global：所有记忆。

        另外，「节点专属记忆」（scope=node）只要地点对得上就会被想起，与模式无关。
        """

        wanted_nodes = [str(item) for item in (nodes or []) if str(item)]
        if node_id and node_id not in wanted_nodes:
            wanted_nodes.append(node_id)
        scoped = list(session_ids or ([session_id] if session_id else []))
        rows: list[dict[str, Any]] = []
        for where in wanted_nodes:
            # 节点专属记忆只属于「这个会话的这个地点」，必须按会话过滤，
            # 否则 A 群的房间记忆会漏进 B 群（真实环境实测到的 bug）。
            node_rows = self.db.query_memories(
                session_ids=scoped or None,
                node_id=where,
                # 空 node_id = 全局记忆（整理/做梦产出的条目），不该被节点过滤挡掉
                include_empty_node=True,
                scope=SCOPE_NODE,
                memory_type=memory_type or None,
                limit=200,
                include_superseded=include_superseded,
            )
            rows.extend(
                item
                for item in node_rows
                # 地点记忆也按人格分桶（WP6 fail-closed）：具名人格读不到无主地点记忆、
                # 无主读不到具名人格的、A 人格读不到 B 人格的。以前这里是
                # `not persona_id or not item.get("persona_id") or ...`，中间那半句
                # **专门给具名人格开了后门**（无主行谁都读得到）；而 db.query_memories
                # 只在传 None 时才不过滤，所以这里用精确相等最省事也最不容易再漏。
                if str(item.get("persona_id") or "") == str(persona_id or "")
            )
        if mode == SCOPE_GLOBAL:
            # 「全局」只表示**放宽会话作用域**（哪个群的记忆都算），不是"绕过后面的筛选"，
            # 也**不是"跨人格"**。以前这里四条 scope 全都不带 persona_id，于是具名人格
            # 能读到别的人格、甚至无主桶的记忆——WP6 的同一个缺口在 global 分支又开了一次。
            for scope in (SCOPE_GLOBAL, SCOPE_GROUP, SCOPE_PERSONA, SCOPE_GROUP_PERSONA):
                rows.extend(
                    self.db.query_memories(
                        scope=scope,
                        persona_id=persona_id,
                        memory_type=memory_type or None,
                        limit=500,
                        include_superseded=include_superseded,
                    )
                )

        if mode in (SCOPE_GROUP, SCOPE_GROUP_PERSONA) and scoped:
            # 会话组共享的是「无主」的 group 记忆；group_persona 里带人格 id 的行只属于
            # 写它的那个人格。persona_id="" 在这里是**精确的空桶过滤**（db.query_memories
            # 只在传 None 时才不过滤），所以：具名人格读不到空桶、空桶读不到具名人格、
            # A 人格读不到 B 人格。以前这里完全不传 persona_id，同一会话组里谁写的
            # 谁都读得到（WP6 修的隐私缺口）。
            rows.extend(
                item
                for item in self.db.query_memories(
                    session_ids=scoped,
                    persona_id=persona_id,
                    memory_type=memory_type or None,
                    limit=500,
                    include_superseded=include_superseded,
                )
                if item.get("scope") in (SCOPE_GROUP, SCOPE_GROUP_PERSONA)
            )
        if mode in (SCOPE_PERSONA, SCOPE_GROUP_PERSONA) and persona_id:
            rows.extend(
                item
                for item in self.db.query_memories(
                    persona_id=persona_id,
                    memory_type=memory_type or None,
                    limit=500,
                    include_superseded=include_superseded,
                )
                if item.get("scope") in (SCOPE_PERSONA, SCOPE_GROUP_PERSONA)
            )
        rows.extend(
            self.db.query_memories(
                scope=SCOPE_GLOBAL,
                # 这条查询**任何模式都会跑**（global 记忆本来就跨会话），所以它也必须按
                # 人格分桶：否则不管把 memory_scope_mode 调成什么，别人格的全局记忆
                # 都还是会漏进来。
                persona_id=persona_id,
                memory_type=memory_type or None,
                limit=200,
                include_superseded=include_superseded,
            )
        )
        if wanted_nodes:
            # 指定了地点/区域就只看这些地方：所有作用域的记忆都带 node_id（写它时的所在地），
            # 不按这个过滤的话，"回忆厨房"会把书房的事也捞进来。
            #
            # 但 node_id 为空的条目是**全局记忆**（整理产出的 gist、做梦的梦，写入时
            # 没有地点），它们不属于任何一个地点，也就不该被地点过滤掉——以前这里
            # `in allowed` 把空串条目 100% 丢掉，而主回复又是唯一带 node_id 的召回路径，
            # 于是"整理出来的记忆一条都召不回"（实测 73/73 条 recall_count=0）。
            allowed = set(wanted_nodes)
            rows = [
                item
                for item in rows
                if str(item.get("node_id") or "") in allowed
                or not str(item.get("node_id") or "")
            ]
        return _dedupe(rows)

    def _score(
        self, item: dict[str, Any], *, focus_user: str, node_id: str, now: float
    ) -> float:
        weight = max(0.0, min(1.0, float(item.get("weight", 0.5))))
        score = weight

        # 用户匹配加成
        related = [str(u) for u in item.get("related_users") or []]
        if focus_user and focus_user in related:
            score *= 1.5

        # 节点匹配加成
        if node_id and item.get("node_id") == node_id:
            score *= 1.3

        # 时间衰减：30 天半衰期
        age_days = max(0.0, (now - float(item.get("created_at", now))) / 86400.0)
        score *= 0.5 ** (age_days / 30.0)

        # 临时召回惩罚：刚被想起过的记忆在窗口内轻一点，出了窗口自动恢复。
        # （以前是按累计次数永久降权，想起来太多次的记忆就再也回不来了。）
        config = self.world.memory if self.world is not None else None
        window = max(60.0, float(getattr(config, "recall_penalty_minutes", 120) or 120) * 60)
        strength = min(0.95, max(0.0, float(getattr(config, "recall_penalty_strength", 0.5) or 0.0)))
        last = float(item.get("last_recalled") or 0.0)
        if last and strength > 0 and now - last < window:
            freshness = 1.0 - max(0.0, now - last) / window
            score *= max(0.05, 1.0 - strength * freshness)

        return score

    @staticmethod
    def _keyword_hits(content: Any, keyword: str) -> int:
        """主题词在内容里命中几个（中文按字/词直接包含即可）。"""

        text = str(content or "")
        words = [word for word in str(keyword or "").replace("，", " ").replace(",", " ").split() if word]
        if not words:
            words = [str(keyword or "").strip()]
        return sum(1 for word in words if word and word in text)

    def _keyword_bonus(self, *, content: Any, keyword: str) -> float:
        hits = self._keyword_hits(content, keyword)
        if not hits:
            return 1.0
        # 命中越多排越前，但不要把分数拉爆
        return 1.0 + min(1.5, 0.5 * hits)

    @staticmethod
    def _to_recalled(score: float, item: dict[str, Any]) -> RecalledMemory:
        return RecalledMemory(
            id=int(item["id"]),
            content=str(item.get("content", "")),
            node_id=str(item.get("node_id", "")),
            memory_type=str(item.get("type", SCENE)),
            emotion=str(item.get("emotion", "")),
            weight=float(item.get("weight", 0.0)),
            score=round(score, 4),
            related_users=[str(u) for u in item.get("related_users") or []],
            created_at=float(item.get("created_at") or 0.0),
            tier=str(item.get("tier") or "raw"),
            context=str(item.get("context") or ""),
            participants=[str(u) for u in (item.get("participants") or [])],
            status=str(item.get("status") or "active"),
            superseded_by=str(item.get("superseded_by") or ""),
        )

    def remember_action(
        self,
        *,
        session_id: str,
        persona_id: str,
        node_id: str,
        label: str,
        weight: float = 0.3,
        window_minutes: int | None = None,
        now: float | None = None,
    ) -> int | None:
        """写一条动作场景记忆：同一处、同一个动作在窗口内**合并成一条**。

        引擎每个 tick 动作成功都会调一次（``在这里发呆`` / ``在这里看书``…）。旧行为
        是每次都新写一行，于是记忆表被流水账灌满，还和真记忆一起抢主回复的 5 个名额。
        现在同 session + 同节点 + 同动作 + ``window_minutes``（默认 30 分钟）内只留
        一条：刷新 ``created_at`` 并把 ``occurrence`` +1，不新增行。

        返回记忆 id（合并时是已有那条的 id）；被临时会话跳过时返回 None。
        """

        text = " ".join(str(label or "").split())
        if not text:
            return None
        config = self.world.memory if self.world is not None else None
        if window_minutes is None:
            try:
                minutes = float(getattr(config, "action_merge_minutes", 30) or 30)
            except (TypeError, ValueError):
                minutes = 30.0
        else:
            minutes = float(window_minutes)
        stamp = float(now if now is not None else time.time())
        content = f"{ACTION_SCENE_PREFIX}{text}"
        since = stamp - max(0.0, minutes) * 60.0
        existing = self.db.find_action_memory(
            session_id=session_id,
            persona_id=persona_id,
            node_id=node_id,
            content=content,
            since=since,
        )
        if existing is not None:
            self.db.touch_action_memory(int(existing["id"]), when=stamp)
            return int(existing["id"])
        return self.remember(
            session_id=session_id,
            persona_id=persona_id,
            node_id=node_id,
            content=content,
            memory_type=SCENE,
            weight=weight,
            source="action",
        )

    # ---------------- 衰减 ----------------

    def decay(self, *, session_id: str, half_life_days: float | None = None) -> int:
        """时间衰减：只把权重压下去，**绝不删除**（配置见 ``memory.decay_*``）。"""

        config = self.world.memory if self.world is not None else None

        def _number(name: str, fallback: float) -> float:
            try:
                return float(getattr(config, name, fallback) or fallback)
            except (TypeError, ValueError):
                return fallback

        return self.db.decay_memories(
            session_id=session_id,
            half_life_days=(
                float(half_life_days)
                if half_life_days is not None
                else _number("decay_half_life_days", 30.0)
            ),
            floor=_number("decay_floor", 0.1),
            idle_days=_number("decay_idle_days", 30.0),
        )

    def tidy(self, *, session_ids: list[str], limit: int = 2000) -> dict[str, Any]:
        """把同一句话记了好几遍的合并掉，返回这次动了多少（用户 m11096 第 16 条）。

        她一天里可能把"今天下过雨"记三回，措辞只差标点。这里按**去空白、去标点、
        转小写**之后的正文分组，每组留权重最高、最新的一条，其余删掉；不跨组合并，
        也不动 pinned（钉住的那条是主人要她记住的，不该被自动整理掉）。
        """

        ids = [str(item) for item in (session_ids or []) if str(item)]
        if not ids:
            return {"checked": 0, "merged": 0, "groups": []}
        rows = self.db.query_memories(session_ids=ids, limit=max(1, int(limit)))
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = _memory_key(str(row.get("content", "")))
            if not key:
                continue
            buckets.setdefault(key, []).append(row)
        merged = 0
        groups: list[dict[str, Any]] = []
        for key, items in buckets.items():
            items.sort(
                key=lambda item: (
                    float(item.get("weight") or 0.0),
                    float(item.get("created_at") or 0.0),
                )
            )
            keep = items[-1]
            drop = [
                int(item["id"])
                for item in items[:-1]
                if not bool(item.get("pinned"))
            ]
            if not drop:
                continue
            self.db.delete_memories_by_ids(drop)
            merged += len(drop)
            groups.append(
                {"kept": int(keep["id"]), "dropped": drop, "content": str(keep.get("content", ""))[:60]}
            )
        return {"checked": len(rows), "merged": merged, "groups": groups}

    def fold(self, memory_id: int, *, text: str = "", when: float | None = None) -> None:
        """折叠一条记忆（raw → gist）：原文进 ``context``，正文换成要点。

        遗忘只做到这一步，**不删除**——人类忘的是细节，不是那件事。
        """

        self.db.fold_memory(int(memory_id), text=text, when=when)

    def due_reviews(
        self,
        *,
        session_ids: list[str],
        persona_id: str = "",
        now: float | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """到点该"想起来一次"的记忆（间隔重复的回访队列）。

        ``persona_id`` 是身份过滤，默认空串 = 只在「无主」桶里翻：无人值守的回访
        （``_tick_session``）手上没有 ctx，宁可按空身份回访，也不能把别人格的记忆
        念出来。具名人格之间同样互不可见。
        """

        stamp = float(now if now is not None else time.time())
        rows = self.db.query_memories(
            session_ids=[str(item) for item in (session_ids or []) if str(item)],
            persona_id=str(persona_id or ""),
            due_review_before=stamp,
            limit=max(1, int(limit)),
            order="next_review_at ASC",
        )
        return rows

    def schedule_review(self, memory_id: int, *, next_at: float) -> None:
        """排下一次回访（1 天 / 3 天 / 7 天 / 30 天……由整理那一步决定）。"""

        self.db.update_memory(int(memory_id), next_review_at=float(next_at))

    # ---------------- 用户控制 ----------------

    def forget_user(self, *, session_id: str, persona_id: str, user_id: str, topic: str = "") -> int:
        return self.db.delete_memories(
            session_id=session_id, persona_id=persona_id, user_id=user_id, topic=topic or None
        )

    def export_user(self, *, session_id: str, persona_id: str, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_memories(
            session_id=session_id,
            persona_id=persona_id,
            limit=5000,
            # 导出是"把她记得的这件事全给你看"：被新说法覆盖的旧说法也算她记得过的，
            # 少了它，导出的档案里"换工作前在哪儿"就凭空消失了。
            include_superseded=True,
        )
        result: list[dict[str, Any]] = []
        for item in rows:
            related = [str(u) for u in item.get("related_users") or []]
            if user_id in related or (not related and user_id in str(item.get("content", ""))):
                result.append(item)
        return result

    def correct(
        self,
        *,
        session_id: str,
        persona_id: str,
        old_text: str,
        new_text: str,
    ) -> int:
        """把包含 old_text 的记忆改写为 new_text。返回修改条数。"""

        rows = self.db.query_memories(
            session_id=session_id, persona_id=persona_id, limit=5000
        )
        changed = 0
        needle = (old_text or "").strip()
        if not needle:
            return 0
        for item in rows:
            content = str(item.get("content", ""))
            if needle in content:
                self.db.update_memory(
                    int(item["id"]), content=content.replace(needle, new_text.strip())
                )
                changed += 1
        return changed

    def stats(self, session_id: str | None = None) -> dict[str, Any]:
        if not session_id:
            return self.db.memory_stats()
        # 会话组：把组里几个会话的记忆合起来看
        return self.db.memory_stats(session_ids=self.sessions(str(session_id)))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _union_text(*groups: Any) -> list[str]:
    """把几份人名/关键词列表并起来（保序、去重、丢掉空值）。"""

    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
    return result


def _is_action_scene(item: dict[str, Any]) -> bool:
    """是不是「在这里…」那条动作流水账。"""

    return str(item.get("type") or "") == SCENE and str(item.get("content") or "").startswith(
        ACTION_SCENE_PREFIX
    )


def _opposite(a: str, b: str) -> bool:
    """粗略的语义相反判断：一条含否定词、另一条不含，且主干相同。"""

    if a == b:
        return False
    a_neg = any(word in a for word in _NEGATION_WORDS)
    b_neg = any(word in b for word in _NEGATION_WORDS)
    if a_neg == b_neg:
        return False
    core_a = _strip_negation(a)
    core_b = _strip_negation(b)
    return bool(core_a) and len(core_a) >= 2 and core_a == core_b


def _strip_negation(text: str) -> str:
    result = text
    for word in _NEGATION_WORDS:
        result = result.replace(word, "")
    return result


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    for item in rows:
        item_id = int(item.get("id", 0))
        if item_id in seen:
            continue
        seen.add(item_id)
        result.append(item)
    return result
