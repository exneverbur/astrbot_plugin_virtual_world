"""用户画像：她认识的每个人（画像 + 事实 + 关系 + 好感度）。

记忆回答"发生过什么"，这一层回答"**这个人是谁、我跟他什么关系、我该怎么对他**"。
设计上的三条硬规矩：

1. **一个写入入口**（:meth:`ProfileStore.note_fact` / :meth:`note_bond`）：
   内置动作、睡眠整理、规则兜底、编辑器手改都走它，去重、证据、关系唯一性都在这儿判；
2. **关系是"候选 + 代码校验"**：模型只提候选，槽位唯一性、冲突策略由代码决定，
   所以不会出现"一句玩笑话让他变成我老公"；
3. **亲密度上限 = min(关系类型给的上限, 好感度达到的级别)**：
   普通关系聊再久也上不去（群友不能亲亲），只有关系本身升级才会放宽。

这一层**不调大模型**：抽取、确认、缩略版都由睡眠整理那一步（core/consolidate.py）负责。
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .db import Database
from .models import BondType, IntimacyLevel, ProfileConfig, WorldConfig

AFFINITY_DAILY_KEY = "affinity_daily"
"""当天的好感额度表（混脸熟 / 每轮模型给的增减，各自有上限）。"""

AFFINITY_DECAY_KEY = "affinity_decay_day"
"""每个会话组最后一次"回落结算"是哪天，保证一天只淡一次。"""

CALL_NAME_CHARS = 12
"""称呼最多几个字：再长基本就是一句话，不是称呼。"""

_CALL_NAME_PUNCT = "。！？!?，,;；、~～…"
_DAY_CACHE = 60
"""记住"来过哪些天"最多留多少天（只用来算认识几天）。"""


def _clean_call_name(value: Any) -> str:
    """把一个称呼洗成人能用的样子：太长、带标点、带链接的一律不算。"""

    text = " ".join(str(value or "").split()).strip()
    text = text.strip("「」『』\"'“”‘’")
    if not text or len(text) > CALL_NAME_CHARS:
        return ""
    if "http" in text.lower() or "@" in text:
        return ""
    if any(char in text for char in _CALL_NAME_PUNCT):
        return ""
    return text


def _norm(text: Any) -> str:
    """归一的比较用文本：去掉空白与常见标点。"""

    return "".join(
        char for char in str(text or "").lower() if char.isalnum()
    )


FACT_SIMILAR_RATIO_LONG = 0.5
"""长句（8 个字以上）相似到这个程度就算同一件事的两种说法。"""
FACT_SIMILAR_RATIO_SHORT = 0.75
"""短句要更像才算：「喜欢猫」和「喜欢狗」相似度就有 0.67，松一点就把两件事并成一件了。"""


def _numbers_in(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\d+", str(text or "")))


def _same_fact(left: Any, right: Any) -> bool:
    """两条事实是不是**同一件事的两种说法**（"明天要去体检" / "明天要去医院体检"）。

    数字必须一致：生日 3 月 5 号和 3 月 6 号长得像，但那是两条不同的事实。
    短句（4 个字以下）不猜——猜错了就是把两件事合成一件，比多留一条更糟。
    """

    a, b = _norm(left), _norm(right)
    if not a or not b:
        return False
    if min(len(a), len(b)) < 4:
        return False
    if _numbers_in(a) != _numbers_in(b):
        return False
    if a in b or b in a:
        return True
    bar = (
        FACT_SIMILAR_RATIO_LONG
        if min(len(a), len(b)) >= 8
        else FACT_SIMILAR_RATIO_SHORT
    )
    return difflib.SequenceMatcher(None, a, b).ratio() >= bar


def _negated(text: str) -> bool:
    """这句话是不是"否定式"（不喜欢 / 不爱 / 别…）。"""

    body = str(text or "")
    for mark in ("不喜欢", "不爱", "讨厌", "别", "不要", "不想", "没", "不"):
        if mark in body:
            return True
    return False


def _conflicts(left: str, right: str) -> bool:
    """两条事实是不是"同一件事、但结论相反"（喜欢辣 / 不喜欢辣）。"""

    a, b = _norm(left), _norm(right)
    if not a or not b:
        return False
    for mark in ("不喜欢", "不爱", "讨厌", "不要", "不想"):
        if (mark in left) != (mark in right):
            body = a.replace(_norm(mark), "")
            other = b.replace(_norm(mark), "")
            if body and other and (body in other or other in body):
                return True
    if _negated(left) != _negated(right):
        body = "".join(ch for ch in a if ch not in "不没别要")
        other = "".join(ch for ch in b if ch not in "不没别要")
        if body and other and (body in other or other in body):
            return True
    return False


@dataclass
class PersonView:
    """提示词渲染用的一份画像视图（把库里的行整理成人话要用的形状）。"""

    user_id: str
    name: str = ""
    affinity: float = 0.0
    level: IntimacyLevel = field(default_factory=IntimacyLevel)
    level_index: int = 0
    """生效档位在 ``levels`` 里的下标（提示词里要拿它说"再熟一点会到哪一档"）。"""

    negative: bool = False
    """这条关系是负面的（讨厌的人 / 敌人）：提示词里要统一压住亲昵与主动。"""

    affinities: list[str] = field(default_factory=list)
    """当前的关系名（可能不止一个：主人 + 男友）。"""

    past: list[dict[str, Any]] = field(default_factory=list)
    """历史关系（type / since / until）。"""

    claims: list[dict[str, Any]] = field(default_factory=list)
    """他自称、她还没认的关系。"""

    facts: list[dict[str, Any]] = field(default_factory=list)
    call_me: str = ""
    call_him: str = ""
    qq_name: str = ""
    cards: dict[str, str] = field(default_factory=dict)
    note: str = ""
    digest: str = ""
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0
    last_talked_at: float = 0.0
    """他上一次**直接跟她说话**是什么时候（0 = 还没正经聊过）。

    和 ``last_seen_at``（在群里露过面就算）分开：提示词里既要能说"你昨天还找过我"，
    也要能说"你在群里说话但没理我"。
    """
    message_count: int = 0
    days: int = 0

    @property
    def days_known(self) -> int:
        return max(1, int(self.days or 1)) if self.first_seen_at else 0


class ProfileStore:
    """用户画像的读写与规则（同步：和记忆层一样，直接用小事务进 SQLite）。"""

    def __init__(
        self,
        db: Database,
        world: WorldConfig | None = None,
        *,
        group_of: Any = None,
    ) -> None:
        self.db = db
        self.world = world
        self._group_of = group_of
        """``session_id -> 组代表会话``：画像按"她"（会话组）存。"""

    # ---------------- 基础 ----------------

    @property
    def config(self) -> ProfileConfig:
        if self.world is None:
            return ProfileConfig()
        return self.world.profile

    def set_world(self, world: WorldConfig) -> None:
        self.world = world

    def group_key(self, session_id: str) -> str:
        """画像存在哪个 key 下：分过组就是组代表会话（同一个她共用一份）。"""

        text = str(session_id or "")
        if self._group_of is None or not text:
            return text
        try:
            return str(self._group_of(text) or text)
        except Exception:
            return text

    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def profile(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        key, uid = self.group_key(session_id), str(user_id or "")
        if not key or not uid:
            return None
        return self.db.get_user_profile(group_id=key, user_id=uid)

    def list_people(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        key = self.group_key(session_id)
        if not key:
            return []
        return self.db.list_user_profiles(group_id=key, limit=limit)

    # ---------------- 平台信息（QQ 号 / 昵称 / 名片）----------------

    def touch(
        self,
        session_id: str,
        user_id: str,
        name: str = "",
        *,
        now: float | None = None,
        count_message: bool = True,
    ) -> dict[str, Any] | None:
        """每条消息都过一遍：更新昵称 / 最后出现 / 聊天条数 / 来过几天。

        第一次见到的人会按配置里的"默认初始关系"建档（默认：陌生人）。
        """

        if not self.enabled():
            return None
        key, uid = self.group_key(session_id), str(user_id or "").strip()
        if not key or not uid:
            return None
        stamp = float(now if now is not None else time.time())
        row = self.db.get_user_profile(group_id=key, user_id=uid)
        payload: dict[str, Any] = dict(row.get("payload") or {}) if row else {}
        names = [str(item) for item in (payload.get("names") or []) if str(item)]
        clean = " ".join(str(name or "").split())
        history = payload.get("name_history") or []
        if clean and (not names or names[-1] != clean):
            names.append(clean)
            if names[:-1]:
                history.append({"name": clean, "at": stamp})
        payload["names"] = names[-8:]
        payload["name_history"] = list(history)[-20:]
        if clean:
            payload["qq_name"] = clean
        days = [str(item) for item in (payload.get("days") or []) if str(item)]
        today = time.strftime("%Y-%m-%d", time.localtime(stamp))
        if today not in days:
            days.append(today)
        payload["days"] = days[-_DAY_CACHE:]
        payload["user_id"] = uid
        first_seen = float(row.get("first_seen_at") or 0.0) if row else 0.0
        if not first_seen:
            first_seen = stamp
        affinity = (
            float(row.get("affinity") or 0.0)
            if row
            else float(self.config.affinity_initial)
        )
        count = int(row.get("message_count") or 0) if row else 0
        digest = str(row.get("digest") or "") if row else ""
        self.db.upsert_user_profile(
            group_id=key,
            user_id=uid,
            payload=payload,
            affinity=affinity,
            first_seen_at=first_seen,
            last_seen_at=stamp,
            message_count=count + (1 if count_message else 0),
            digest=digest,
        )
        if row is None:
            self.ensure_default_bond(session_id, uid, now=stamp)
        return self.db.get_user_profile(group_id=key, user_id=uid)

    def set_card(self, session_id: str, user_id: str, card: str) -> bool:
        """记下他在这个群的群名片（协议端给得到时才写）。"""

        key, uid = self.group_key(session_id), str(user_id or "")
        text = " ".join(str(card or "").split())
        if not key or not uid or not text:
            return False
        row = self.db.get_user_profile(group_id=key, user_id=uid)
        if row is None:
            row = self.touch(session_id, uid, count_message=False)
        if row is None:
            return False
        payload = dict(row.get("payload") or {})
        cards = dict(payload.get("cards") or {})
        if cards.get(str(session_id)) == text:
            return False
        cards[str(session_id)] = text
        payload["cards"] = cards
        self._save(key, uid, payload)
        return True

    def set_note(self, session_id: str, user_id: str, note: str) -> None:
        """主人手写的备注（不进模型，只显示）。"""

        key, uid = self.group_key(session_id), str(user_id or "")
        row = self.db.get_user_profile(group_id=key, user_id=uid)
        if row is None:
            return
        payload = dict(row.get("payload") or {})
        payload["note"] = " ".join(str(note or "").split())[:200]
        self._save(key, uid, payload)

    def set_call_names(
        self,
        session_id: str,
        user_id: str,
        *,
        call_me: str = "",
        call_him: str = "",
        explicit: bool = True,
    ) -> dict[str, Any]:
        """记称呼：``call_me`` 是他让她怎么称呼自己，``call_him`` 是她怎么称呼他。

        只有"他明确说"（``explicit``）才允许覆盖已有的称呼——不然群里的一句玩笑
        会把称呼改得乱七八糟；黑名单里的称呼直接拒绝。
        """

        key, uid = self.group_key(session_id), str(user_id or "")
        row = self.db.get_user_profile(group_id=key, user_id=uid)
        if row is None:
            row = self.touch(session_id, uid)
        if row is None:
            return {"ok": False, "reason": "还没有这个人的画像"}
        payload = dict(row.get("payload") or {})
        blocked = {str(item) for item in (self.config.call_name_blacklist or [])}
        result: dict[str, Any] = {"ok": True, "changed": [], "rejected": []}
        for field_name, value in (("call_me", call_me), ("call_him", call_him)):
            clean = _clean_call_name(value)
            if not clean:
                continue
            if clean in blocked:
                result["rejected"].append({"field": field_name, "value": clean, "reason": "黑名单"})
                continue
            current = str(payload.get(field_name) or "")
            if current and current != clean and not explicit:
                result["rejected"].append(
                    {"field": field_name, "value": clean, "reason": "已有称呼，只有他明确改才换"}
                )
                continue
            if current == clean:
                continue
            payload[field_name] = clean
            history = [
                item for item in (payload.get(f"{field_name}_history") or []) if isinstance(item, dict)
            ]
            history.append({"value": clean, "at": time.time()})
            payload[f"{field_name}_history"] = history[-10:]
            result["changed"].append({"field": field_name, "value": clean})
        if result["changed"]:
            self._save(key, uid, payload)
        return result

    # ---------------- 事实 ----------------

    def note_fact(
        self,
        session_id: str,
        user_id: str,
        *,
        text: str,
        kind: str = "other",
        evidence: str = "",
        context: str = "",
        confidence: float = 0.6,
        status: str = "active",
        source_session: str = "",
        mentions: list[str] | None = None,
        pinned: bool = False,
    ) -> dict[str, Any]:
        """记一条关于他的事实（去重 / 冲突处理都在这儿）。

        返回 ``{"ok", "id", "action", "reason"}``；``action`` 是 ``add`` / ``merge`` /
        ``replace`` / ``skip``，方便整理日志写清楚发生了什么。
        """

        if not self.enabled():
            return {"ok": False, "action": "skip", "reason": "画像功能关着"}
        key, uid = self.group_key(session_id), str(user_id or "").strip()
        body = " ".join(str(text or "").split())[:80]
        if not key or not uid or not body:
            return {"ok": False, "action": "skip", "reason": "没有内容"}
        kind = str(kind or "other")
        existing = self.db.list_user_facts(group_id=key, user_id=uid)
        for item in existing:
            if str(item.get("kind") or "") != kind:
                continue
            if _norm(item.get("text")) == _norm(body) or _same_fact(item.get("text"), body):
                # 同一件事的另一种说法也合并：不然"他明天要去体检"能攒六条，
                # 把「关于他 / 他喜欢 / 他的习惯」那几行的 6 个位置全挤掉。
                old = str(item.get("text") or "")
                self.db.update_user_fact(
                    fact_id=int(item["id"]),
                    status="active",
                    # 留信息更全的那一版（新说的通常更长）
                    text=body if len(body) > len(old) else None,
                    pinned=True if pinned else None,
                    confidence=max(float(item.get("confidence") or 0.0), float(confidence)),
                    last_confirmed_at=time.time(),
                )
                return {"ok": True, "id": int(item["id"]), "action": "merge"}
            if _conflicts(str(item.get("text") or ""), body):
                self.db.update_user_fact(fact_id=int(item["id"]), status="past")
                new_id = self.db.add_user_fact(
                    group_id=key,
                    user_id=uid,
                    kind=kind,
                    text=body,
                    evidence=evidence,
                    context=context,
                    confidence=confidence,
                    status=status,
                    source_session=source_session,
                    pinned=pinned,
                    mentions=mentions,
                )
                return {"ok": True, "id": new_id, "action": "replace"}
        new_id = self.db.add_user_fact(
            group_id=key,
            user_id=uid,
            kind=kind,
            text=body,
            evidence=evidence,
            context=context,
            confidence=confidence,
            status=status,
            source_session=source_session,
            pinned=pinned,
            mentions=mentions,
        )
        return {"ok": True, "id": new_id, "action": "add"}

    def facts(self, session_id: str, user_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        key, uid = self.group_key(session_id), str(user_id or "")
        return self.db.list_user_facts(group_id=key, user_id=uid, **kwargs)

    def dedupe_facts(self, session_id: str, user_id: str) -> int:
        """把同一个人的同类事实里**重复的说法**合并掉，返回合并了几条。

        清理存量用的：同一件事被换着说法记了六遍，那一行（每类最多 6 条）
        就全是废话，别的信息全被挤出去。保留置顶的、信息更全的那条，
        其余记成"曾经"——不删，只是不再占位置。
        """

        key, uid = self.group_key(session_id), str(user_id or "")
        if not key or not uid:
            return 0
        rows = self.db.list_user_facts(group_id=key, user_id=uid)
        merged = 0
        for kind in {str(item.get("kind") or "") for item in rows}:
            group = [item for item in rows if str(item.get("kind") or "") == kind]
            # 先排序：置顶优先、信息更全的优先——留下的就是这几条里最该留的
            ordered = sorted(
                group,
                key=lambda item: (
                    bool(item.get("pinned")),
                    len(str(item.get("text") or "")),
                    float(item.get("last_confirmed_at") or 0.0),
                ),
                reverse=True,
            )
            kept: list[dict[str, Any]] = []
            for item in ordered:
                if any(_same_fact(one.get("text"), item.get("text")) for one in kept):
                    self.db.update_user_fact(fact_id=int(item["id"]), status="past")
                    merged += 1
                    continue
                kept.append(item)
        return merged

    # ---------------- 关系 ----------------

    def ensure_default_bond(
        self, session_id: str, user_id: str, *, now: float | None = None
    ) -> dict[str, Any]:
        """第一次见到他：先挂上默认初始关系（默认"陌生人"）。"""

        name = str(self.config.default_bond or "陌生人")
        bond = self.config.bond_by_name(name)
        if bond is None:
            return {"ok": False, "action": "skip", "reason": "默认关系不在关系表里"}
        existing = [
            item
            for item in self.db.list_user_bonds(
                group_id=self.group_key(session_id), user_id=str(user_id), statuses=["current"]
            )
        ]
        if existing:
            return {"ok": True, "action": "skip", "reason": "已经有关系了"}
        return self.note_bond(
            session_id,
            user_id,
            type=bond.name,
            evidence="第一次见到他",
            confidence=1.0,
            asserted_by="她的判断",
            now=now,
        )

    def note_bond(
        self,
        session_id: str,
        user_id: str,
        *,
        type: str,
        evidence: str = "",
        confidence: float = 0.6,
        asserted_by: str = "她的判断",
        now: float | None = None,
        policy: str = "",
    ) -> dict[str, Any]:
        """确立 / 更新一条关系（模型给的候选也走这里）。

        冲突策略（``ProfileConfig.bond_conflict_policy``，默认 ``reject``）：

        - ``reject``：不改关系，把这句话记成 ``claimed``（"他自称"）；
        - ``replace``：新的顶掉同槽位的旧关系，旧的转成"曾经"；
        - ``ask``：也记成 ``claimed``，并在画像里留一句"她在犹豫"。
        """

        if not self.enabled():
            return {"ok": False, "action": "skip", "reason": "画像功能关着"}
        key, uid = self.group_key(session_id), str(user_id or "").strip()
        stamp = float(now if now is not None else time.time())
        bond = self.config.bond_by_name(type)
        if not key or not uid:
            return {"ok": False, "action": "skip", "reason": "缺少会话或人"}
        if bond is None:
            return {"ok": False, "action": "skip", "reason": f"认不出这种关系：{type}"}
        current = self.db.list_user_bonds(group_id=key, user_id=uid, statuses=["current"])
        claimed = self.db.list_user_bonds(group_id=key, user_id=uid, statuses=["claimed"])
        for item in current:
            if str(item.get("type")) == str(bond.name):
                return {"ok": True, "action": "merge", "id": int(item["id"])}
        # 同一个槽位已经有人的：按策略处理（唯一的关系才会管）。
        # 注意槽位唯一性是**全组**的：别人已经占着"男友"时，新来的同样要按策略处理——
        # 不然一个人可以同时有五个男友。
        everyone = self.db.list_user_bonds(group_id=key, statuses=["current"])
        blocker = next(
            (
                item
                for item in everyone
                if str(item.get("slot")) == str(bond.slot)
                and self._unique_slot(str(bond.slot))
            ),
            None,
        )
        claimed_by_him = str(asserted_by or "") in ("他自称", "他自己说的", "user")
        if blocker is not None:
            mode = str(policy or getattr(self.config, "bond_conflict_policy", "reject") or "reject")
            if mode == "replace" and not claimed_by_him:
                self.db.close_user_bond(bond_id=int(blocker["id"]), until=stamp)
            else:
                existing_claim = next(
                    (
                        item
                        for item in claimed
                        if str(item.get("type")) == str(bond.name)
                    ),
                    None,
                )
                if existing_claim is not None:
                    return {"ok": True, "action": "merge", "id": int(existing_claim["id"])}
                new_id = self.db.add_user_bond(
                    group_id=key,
                    user_id=uid,
                    type=bond.name,
                    slot=bond.slot,
                    status="claimed",
                    since=stamp,
                    evidence=evidence,
                    confidence=confidence,
                    asserted_by=asserted_by or "他自称",
                )
                return {
                    "ok": True,
                    "action": "claim",
                    "id": new_id,
                    "reason": f"已经有一个「{blocker.get('type')}」"
                    f"（{blocker.get('user_id')}），这句先记成他自称",
                }
        new_id = self.db.add_user_bond(
            group_id=key,
            user_id=uid,
            type=bond.name,
            slot=bond.slot,
            status="current",
            since=stamp,
            evidence=evidence,
            confidence=confidence,
            asserted_by=asserted_by,
        )
        self._retire_default_bond(key, uid, keep_slot=str(bond.slot), now=stamp)
        self._retire_group_bonds(key, uid, bond=bond, now=stamp)
        self.lift_affinity_to_bond_floor(session_id, uid, bond, now=stamp)
        return {"ok": True, "action": "add", "id": new_id}

    def lift_affinity_to_bond_floor(
        self, session_id: str, user_id: str, bond: Any, *, now: float | None = None
    ) -> float:
        """绑上一条关系之后，把好感抬到「这个关系最低档」的下边界。返回抬了多少。

        关系本身是一种态度：绑了男友就不该还停在"陌生人"那份好感上。
        **只往上抬**——好感已经比它高的说明是相处养出来的，一律不动。
        抬起来的那一下会记一条好感变化（来源写"绑上关系"），在通讯录里看得见。
        """

        levels = list(getattr(self.config, "levels", None) or [])
        if bond is None or not levels:
            return 0.0
        index = max(0, min(int(getattr(bond, "floor", 0) or 0), len(levels) - 1))
        target = float(getattr(levels[index], "min_affinity", 0.0) or 0.0)
        key, uid = self.group_key(session_id), str(user_id or "")
        row = self.db.get_user_profile(group_id=key, user_id=uid)
        if row is None:
            return 0.0
        current = float(row.get("affinity") or 0.0)
        if current >= target:
            return 0.0
        self.adjust_affinity(
            session_id,
            uid,
            target - current,
            reason=f"绑上关系「{str(getattr(bond, 'name', '') or '')}」",
            source="bond_floor",
            now=now,
        )
        return target - current

    def accept_claim(
        self, session_id: str, user_id: str, *, bond_id: int, policy: str = ""
    ) -> dict[str, Any]:
        """把她还没认的那条（claimed）认下来：同槽位的关系按策略让位。"""

        key, uid = self.group_key(session_id), str(user_id or "")
        row = self.db.get_user_bond(bond_id=int(bond_id))
        if row is None or str(row.get("group_id")) != key or str(row.get("user_id")) != uid:
            return {"ok": False, "reason": "找不到这条关系"}
        slot = str(row.get("slot") or "")
        mode = str(policy or getattr(self.config, "bond_conflict_policy", "reject") or "replace")
        if self._unique_slot(slot):
            # 槽位唯一性是全组的：占着这个槽位的可能是别人
            for item in self.db.list_user_bonds(group_id=key, statuses=["current"]):
                if str(item.get("slot")) != slot:
                    continue
                if mode == "reject":
                    return {
                        "ok": False,
                        "reason": f"已经有「{item.get('type')}」（{item.get('user_id')}），先解除它",
                    }
                self.db.close_user_bond(bond_id=int(item["id"]), until=time.time())
        self._execute_bond_accept(bond_id=int(bond_id))
        return {"ok": True}

    def _execute_bond_accept(self, *, bond_id: int) -> None:
        row = self.db.get_user_bond(bond_id=bond_id)
        if row is None:
            return
        self.db.close_user_bond(bond_id=bond_id, until=0.0, status="current")
        now = time.time()
        self._retire_default_bond(
            str(row.get("group_id") or ""),
            str(row.get("user_id") or ""),
            keep_slot=str(row.get("slot") or ""),
            now=now,
        )
        self._retire_group_bonds(
            str(row.get("group_id") or ""),
            str(row.get("user_id") or ""),
            bond=self.config.bond_by_name(str(row.get("type") or "")),
            now=now,
        )
        # 「他自称是你男友 → 你认了」也是一次绑定：好感同样要够这一档的最低值
        self.lift_affinity_to_bond_floor(
            str(row.get("group_id") or ""),
            str(row.get("user_id") or ""),
            self.config.bond_by_name(str(row.get("type") or "")),
            now=now,
        )

    def bond_group(self, name: str) -> str:
        """这条关系属于哪一类（``group`` 留空 = 跟谁都不互斥）。"""

        bond = self.config.bond_by_name(name)
        return str(getattr(bond, "group", "") or "") if bond is not None else ""

    def _retire_group_bonds(
        self, group_id: str, user_id: str, *, bond: Any, now: float
    ) -> None:
        """同类关系只留一条：她判断成新的那条之后，旧的自动变成"曾经"。

        这一类管的是"同一个人的几种关系互相替代"：群友 → 朋友 → 男友 是升级，
        男友 → 朋友 是她自己想清楚了（主动退回），两种都按"最新的判断说了算"处理，
        旧的那条带着起止日期留进"曾经"，提示词里就能写成"你们从朋友变成男友了"。
        主人 / 家人这类身份关系不填 ``group``，照旧能跟亲密关系并存。
        """

        group = str(getattr(bond, "group", "") or "")
        uid = str(user_id or "")
        if not group or not group_id or not uid or bond is None:
            return
        keep = str(getattr(bond, "name", "") or "")
        for item in self.db.list_user_bonds(
            group_id=group_id, user_id=uid, statuses=["current"]
        ):
            if str(item.get("type") or "") == keep:
                continue
            if self.bond_group(str(item.get("type") or "")) != group:
                continue
            self.db.close_user_bond(bond_id=int(item["id"]), until=now, status="past")

    def _initial_slot(self) -> str:
        """默认初始关系（"陌生人"）所在的槽位。

        只认 ``default_bond``：它同时也是"第一次见到他先挂哪一条"的来源。
        老配置里那个 ``bonds[].initial`` 标记在读配置时就搬进来了（见 ProfileConfig）。
        """

        bond = self.config.bond_by_name(str(self.config.default_bond or ""))
        return str(bond.slot) if bond is not None else ""

    def _retire_default_bond(
        self, group_id: str, user_id: str, *, keep_slot: str, now: float
    ) -> None:
        """有了正经关系之后，就不该还挂着"陌生人"。

        默认初始关系是"第一次见到他"时垫的底，不是她真的判断。
        别人把她当主人、当朋友之后，通讯录里还并列一行"陌生人"会让
        提示词自相矛盾（"你们还不熟"和"他是你主人"同时出现）。
        """

        slot = self._initial_slot()
        uid = str(user_id or "")
        if not slot or not group_id or not uid or slot == str(keep_slot or ""):
            return
        for item in self.db.list_user_bonds(
            group_id=group_id, user_id=uid, statuses=["current"]
        ):
            if str(item.get("slot")) == slot:
                self.db.close_user_bond(
                    bond_id=int(item["id"]), until=now, status="past"
                )

    # ---------------- 好感的"日常"三条：混脸熟 / 每轮判断 / 慢慢回落 ----------------

    def _daily_people(self, group_id: str, day: str) -> dict[str, dict[str, float]]:
        """当天这份"额度表"（按会话组存，几个会话共用一个她）。"""

        raw = self._kv_store()
        stored = raw.kv_get(AFFINITY_DAILY_KEY, {}) if raw is not None else {}
        data = dict(stored) if isinstance(stored, dict) else {}
        entry = data.get(group_id)
        if not isinstance(entry, dict) or str(entry.get("day") or "") != day:
            entry = {"day": day, "people": {}}
            data[group_id] = entry
            if raw is not None:
                raw.kv_set(AFFINITY_DAILY_KEY, data)
        people = entry.get("people")
        if not isinstance(people, dict):
            people = {}
            entry["people"] = people
        return people

    def _save_daily_people(
        self, group_id: str, day: str, people: dict[str, Any]
    ) -> None:
        raw = self._kv_store()
        if raw is None:
            return
        stored = raw.kv_get(AFFINITY_DAILY_KEY, {})
        data = dict(stored) if isinstance(stored, dict) else {}
        data[group_id] = {"day": day, "people": people}
        raw.kv_set(AFFINITY_DAILY_KEY, data)

    @staticmethod
    def _day_key(now: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(float(now)))

    def _kv_store(self) -> Any:
        """拿"能读写 kv 的那个对象"：异步库的 ``raw``，或者同步库自己。"""

        raw = getattr(self.db, "raw", None)
        if raw is not None and hasattr(raw, "kv_get"):
            return raw
        return self.db if hasattr(self.db, "kv_get") else None

    def note_contact(self, session_id: str, user_id: str, *, now: float | None = None) -> float:
        """「混脸熟」：他出现一次就加一点好感（不用 @ 她）。

        同一人默认 10 分钟内只算一次（连发一屏也只加一次），每天有上限；
        日志只在**当天第一次**写一条，免得日志页被 0.05 刷满。
        """

        step = abs(float(getattr(self.config, "presence_affinity", 0.0) or 0.0))
        if not self.enabled() or step <= 0:
            return 0.0
        stamp = float(now if now is not None else time.time())
        key, uid = self.group_key(session_id), str(user_id or "").strip()
        if not key or not uid:
            return 0.0
        if self.db.get_user_profile(group_id=key, user_id=uid) is None:
            return 0.0  # 只给已经建过档的人加
        day = self._day_key(stamp)
        people = self._daily_people(key, day)
        bucket = dict(people.get(uid) or {})
        cooldown = max(0, int(getattr(self.config, "presence_cooldown_minutes", 10) or 0)) * 60
        last = float(bucket.get("seen_at") or 0.0)
        if cooldown and last and stamp - last < cooldown:
            return 0.0
        used = float(bucket.get("seen") or 0.0)
        cap = abs(float(getattr(self.config, "presence_daily_max", 0.0) or 0.0))
        amount = step if cap <= 0 else max(0.0, min(step, cap - used))
        bucket["seen"] = used + amount
        bucket["seen_at"] = stamp
        people[uid] = bucket
        self._save_daily_people(key, day, people)
        if amount <= 0:
            return 0.0
        self.adjust_affinity(
            session_id,
            uid,
            amount,
            reason="混脸熟",
            source="presence",
            now=stamp,
            log=used <= 1e-9,  # 当天第一条才写日志
        )
        return amount

    def apply_reply_affinity(
        self,
        session_id: str,
        user_id: str,
        delta: float,
        *,
        reason: str = "",
        now: float | None = None,
    ) -> float:
        """主模型每轮给的 ``affinity_delta``：先削到每轮上限，再受"每天每人"约束。

        加与减各有独立的每日额度：**私聊连着聊、或者被反复 @ 也不会一路涨满**。
        """

        amount = float(delta or 0.0)
        if not self.enabled() or abs(amount) < 1e-9:
            return 0.0
        per_turn = abs(float(getattr(self.config, "affinity_reply_max", 3.0) or 0.0))
        if per_turn > 0:
            amount = max(-per_turn, min(per_turn, amount))
        stamp = float(now if now is not None else time.time())
        key, uid = self.group_key(session_id), str(user_id or "").strip()
        if not key or not uid:
            return 0.0
        if self.db.get_user_profile(group_id=key, user_id=uid) is None:
            return 0.0
        day = self._day_key(stamp)
        people = self._daily_people(key, day)
        bucket = dict(people.get(uid) or {})
        cap = abs(float(getattr(self.config, "affinity_daily_max", 0.0) or 0.0))
        if cap > 0:
            if amount > 0:
                room = cap - float(bucket.get("model_up") or 0.0)
                amount = max(0.0, min(amount, room))
                bucket["model_up"] = float(bucket.get("model_up") or 0.0) + amount
            else:
                room = cap - float(bucket.get("model_down") or 0.0)
                amount = -max(0.0, min(-amount, room))
                bucket["model_down"] = float(bucket.get("model_down") or 0.0) + abs(amount)
        people[uid] = bucket
        self._save_daily_people(key, day, people)
        if abs(amount) < 1e-9:
            return 0.0
        self.adjust_affinity(
            session_id,
            uid,
            amount,
            reason=reason or "聊了这一轮",
            source="chat",
            now=stamp,
        )
        return amount

    def decay_affinity(
        self, session_id: str, *, now: float | None = None
    ) -> list[tuple[str, float]]:
        """每天一次：所有人的好感朝 0 回落一点（长期不理就慢慢淡）。

        每个会话组一天只结算一次（几个群私聊共享一份画像，也只该淡一次）；
        落在"朝 0 靠拢"上，所以负好感也会慢慢被忘掉。
        """

        step = abs(float(getattr(self.config, "affinity_decay_per_day", 0.0) or 0.0))
        if not self.enabled() or step <= 0:
            return []
        stamp = float(now if now is not None else time.time())
        day = self._day_key(stamp)
        key = self.group_key(session_id)
        raw = self._kv_store()
        if not key or raw is None:
            return []
        marks = raw.kv_get(AFFINITY_DECAY_KEY, {})
        marks = dict(marks) if isinstance(marks, dict) else {}
        if str(marks.get(key) or "") == day:
            return []
        changed: list[tuple[str, float]] = []
        for row in self.list_people(session_id, limit=500):
            uid = str(row.get("user_id") or "")
            value = float(row.get("affinity") or 0.0)
            if not uid or abs(value) < 1e-9:
                continue
            amount = -step if value > 0 else step
            if abs(amount) > abs(value):
                amount = -value
            self.adjust_affinity(
                session_id,
                uid,
                amount,
                reason="好久没见，慢慢淡了点",
                source="decay",
                now=stamp,
            )
            changed.append((uid, round(float(amount), 3)))
        marks[key] = day
        raw.kv_set(AFFINITY_DECAY_KEY, marks)
        return changed

    # ---------------- 她主动找人的额度 ----------------

    def proactive_quota_left(
        self, session_id: str, user_id: str, *, now: float | None = None
    ) -> int:
        """今天还能**主动**去找他几次。

        上限来自这个人的**当前亲密度级别**（分级表里的「每天最多主动找他几次」）：
        陌生人 / 客气是 0，熟人 1、朋友 2、亲近 3、特别的人 4——越亲近越会主动去找他。
        """

        if not self.enabled():
            return 0
        view = self.view(session_id, user_id)
        if view is None:
            return 0
        cap = max(0, int(getattr(view.level, "proactive_per_day", 0) or 0))
        if cap <= 0:
            return 0
        stamp = float(now if now is not None else time.time())
        key, uid = self.group_key(session_id), str(user_id or "").strip()
        if not key or not uid:
            return 0
        people = self._daily_people(key, self._day_key(stamp))
        used = int((people.get(uid) or {}).get("proactive") or 0)
        return max(0, cap - used)

    def note_proactive(
        self, session_id: str, user_id: str, *, now: float | None = None
    ) -> None:
        """记一次"她主动找他"（每天按人计数）。"""

        stamp = float(now if now is not None else time.time())
        key, uid = self.group_key(session_id), str(user_id or "").strip()
        if not key or not uid:
            return
        day = self._day_key(stamp)
        people = self._daily_people(key, day)
        bucket = dict(people.get(uid) or {})
        bucket["proactive"] = int(bucket.get("proactive") or 0) + 1
        people[uid] = bucket
        self._save_daily_people(key, day, people)

    def close_bond(self, session_id: str, user_id: str, *, bond_id: int) -> bool:
        """解除一条关系（她甩人 / 绝交）：保留成"曾经"。"""

        key, uid = self.group_key(session_id), str(user_id or "")
        row = self.db.get_user_bond(bond_id=int(bond_id))
        if row is None or str(row.get("group_id")) != key or str(row.get("user_id")) != uid:
            return False
        return self.db.close_user_bond(bond_id=int(bond_id), until=time.time(), status="past")

    def bonds(self, session_id: str, user_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        key, uid = self.group_key(session_id), str(user_id or "")
        return self.db.list_user_bonds(group_id=key, user_id=uid, **kwargs)

    def visible_bonds(
        self, session_id: str, user_id: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """对外展示的关系：把垫底的默认关系、以及老数据里同类并排的几条收干净。

        只影响"给她看 / 给界面看"的那一份，不动库：老版本可能给同一个人同时留着
        "朋友"和"男友"两条当前关系，展示时只留最有分量的那条（``cap`` 高的；
        一样高就取更晚确立的），免得提示词里自相矛盾、通讯录里也看不出到底算什么。
        """

        rows = self.bonds(session_id, user_id, **kwargs)
        current = [row for row in rows if str(row.get("status")) == "current"]
        default_name = str(self.config.default_bond or "")
        hidden: set[int] = set()
        if default_name and any(
            str(row.get("type") or "") != default_name for row in current
        ):
            # 被顶掉的那条"陌生人"是垫底的默认值，不是真的过去关系
            hidden.update(
                int(row.get("id") or 0)
                for row in current
                if str(row.get("type") or "") == default_name
            )
            current = [
                row
                for row in current
                if str(row.get("type") or "") != default_name
            ]
        by_group: dict[str, list[dict[str, Any]]] = {}
        for row in current:
            group = self.bond_group(str(row.get("type") or ""))
            if group:
                by_group.setdefault(group, []).append(row)
        for items in by_group.values():
            if len(items) <= 1:
                continue
            best = max(
                items,
                key=lambda row: (
                    self._bond_cap(str(row.get("type") or "")),
                    float(row.get("since") or 0.0),
                ),
            )
            for row in items:
                if row is not best:
                    hidden.add(int(row.get("id") or 0))
        visible = [
            row
            for row in rows
            if int(row.get("id") or 0) not in hidden
            and not (
                str(row.get("status")) == "past"
                and default_name
                and str(row.get("type") or "") == default_name
            )
        ]
        return visible

    def _bond_cap(self, name: str) -> int:
        bond = self.config.bond_by_name(name)
        return int(getattr(bond, "cap", 0) or 0) if bond is not None else 0

    def _unique_slot(self, slot: str) -> bool:
        """这个槽位是不是"只能有一个人"（配置里任一种关系标了 unique 就算）。"""

        text = str(slot or "")
        for bond in self.config.bonds:
            if str(bond.slot) == text:
                return bool(bond.unique)
        return False

    # ---------------- 好感度 ----------------

    def adjust_affinity(
        self,
        session_id: str,
        user_id: str,
        delta: float,
        *,
        reason: str = "",
        source: str = "",
        now: float | None = None,
        log: bool = True,
    ) -> dict[str, Any]:
        """加减好感度：夹在配置的范围里，并记一条变化日志。"""

        if not self.enabled():
            return {"ok": False, "value": 0.0, "reason": "画像功能关着"}
        key, uid = self.group_key(session_id), str(user_id or "")
        row = self.db.get_user_profile(group_id=key, user_id=uid)
        if row is None:
            row = self.touch(session_id, uid, now=now, count_message=False)
        if row is None:
            return {"ok": False, "value": 0.0, "reason": "没有这个人的画像"}
        before = float(row.get("affinity") or 0.0)
        low = float(self.config.affinity_min)
        high = float(self.config.affinity_max)
        after = max(low, min(high, before + float(delta or 0.0)))
        self.db.upsert_user_profile(
            group_id=key,
            user_id=uid,
            payload=dict(row.get("payload") or {}),
            affinity=after,
            first_seen_at=float(row.get("first_seen_at") or 0.0),
            last_seen_at=float(row.get("last_seen_at") or 0.0),
            message_count=int(row.get("message_count") or 0),
            digest=str(row.get("digest") or ""),
        )
        if log and abs(after - before) > 1e-6:
            self.db.add_affinity_log(
                group_id=key,
                user_id=uid,
                delta=after - before,
                value_after=after,
                reason=reason,
                source=source,
                at=now,
            )
        return {"ok": True, "value": after, "delta": after - before}

    def affinity_logs(self, session_id: str, user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        key, uid = self.group_key(session_id), str(user_id or "")
        return self.db.list_affinity_logs(group_id=key, user_id=uid, limit=limit)

    def note_talked(
        self, session_id: str, user_id: str, *, now: float | None = None
    ) -> None:
        """记下"他这次是**直接跟她说话**"（而不是只在群里露了个面）。

        「想念」看的是这个时间：他天天在群里聊天、但从没找过她，她照样会想他。
        """

        key, uid = self.group_key(session_id), str(user_id or "").strip()
        if not key or not uid:
            return
        row = self.db.get_user_profile(group_id=key, user_id=uid)
        if row is None:
            row = self.touch(session_id, uid, now=now, count_message=False)
        if row is None:
            return
        payload = dict(row.get("payload") or {})
        payload["last_talked_at"] = float(now if now is not None else time.time())
        self._save(key, uid, payload)

    def last_talked_at(self, session_id: str, user_id: str) -> float:
        """他上一次**直接跟她说话**是什么时候（0 = 还没聊过）。"""

        row = self.profile(session_id, user_id)
        payload = dict((row or {}).get("payload") or {})
        try:
            return float(payload.get("last_talked_at") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def set_digest(self, session_id: str, user_id: str, digest: str) -> None:
        """写缩略版画像（睡眠整理生成的，非主要的人注入这一份）。"""

        key, uid = self.group_key(session_id), str(user_id or "")
        row = self.db.get_user_profile(group_id=key, user_id=uid)
        if row is None:
            return
        limit = max(10, int(self.config.digest_chars))
        text = " ".join(str(digest or "").split())[:limit]
        self.db.upsert_user_profile(
            group_id=key,
            user_id=uid,
            payload=dict(row.get("payload") or {}),
            affinity=float(row.get("affinity") or 0.0),
            first_seen_at=float(row.get("first_seen_at") or 0.0),
            last_seen_at=float(row.get("last_seen_at") or 0.0),
            message_count=int(row.get("message_count") or 0),
            digest=text,
        )

    # ---------------- 视图 ----------------

    def view(self, session_id: str, user_id: str, *, limit_facts: int = 40) -> PersonView | None:
        """把库里的行整理成提示词要用的视图（关系、分级、事实都算好）。"""

        row = self.profile(session_id, user_id)
        if row is None:
            return None
        payload = dict(row.get("payload") or {})
        key, uid = self.group_key(session_id), str(user_id or "")
        bonds = self.visible_bonds(session_id, user_id)
        current = [item for item in bonds if str(item.get("status")) == "current"]
        past = [item for item in bonds if str(item.get("status")) == "past"]
        claims = [item for item in bonds if str(item.get("status")) == "claimed"]
        affinity = float(row.get("affinity") or 0.0)
        bond_names = [str(item.get("type") or "") for item in current]
        cap = self.config.bond_cap(bond_names)
        floor = self.config.bond_floor(bond_names)
        level = self.config.level_for(affinity, cap=cap, floor=floor)
        level_index = self.config.level_index_for(affinity, cap=cap, floor=floor)
        names = [str(item) for item in (payload.get("names") or []) if str(item)]
        return PersonView(
            user_id=uid,
            name=names[-1] if names else str(payload.get("qq_name") or uid),
            affinity=affinity,
            level=level,
            level_index=level_index,
            negative=any(
                bool(getattr(bond, "negative", False))
                for bond in (self.config.bond_by_name(name) for name in bond_names)
                if bond is not None
            ),
            affinities=[str(item.get("type") or "") for item in current],
            past=past,
            claims=claims,
            facts=self.db.list_user_facts(
                group_id=key, user_id=uid, statuses=["active"], limit=limit_facts
            ),
            call_me=str(payload.get("call_me") or ""),
            call_him=str(payload.get("call_him") or ""),
            qq_name=str(payload.get("qq_name") or ""),
            cards={str(k): str(v) for k, v in (payload.get("cards") or {}).items()},
            note=str(payload.get("note") or ""),
            digest=str(row.get("digest") or ""),
            first_seen_at=float(row.get("first_seen_at") or 0.0),
            last_seen_at=float(row.get("last_seen_at") or 0.0),
            last_talked_at=float(payload.get("last_talked_at") or 0.0),
            message_count=int(row.get("message_count") or 0),
            days=len([item for item in (payload.get("days") or []) if str(item)]),
        )

    def action_label(self, action_id: str) -> str:
        """动作 id 说成人话（提示词里写"现在还不能：抱抱 / 亲亲"）。"""

        if self.world is None:
            return str(action_id)
        definition = self.world.action_map().get(str(action_id))
        return (definition.name or definition.id) if definition else str(action_id)

    # ---------------- 内部 ----------------

    def _save(self, group_id: str, user_id: str, payload: dict[str, Any]) -> None:
        row = self.db.get_user_profile(group_id=group_id, user_id=user_id)
        self.db.upsert_user_profile(
            group_id=group_id,
            user_id=user_id,
            payload=payload,
            affinity=float(row.get("affinity") or 0.0) if row else float(
                self.config.affinity_initial
            ),
            first_seen_at=float(row.get("first_seen_at") or 0.0) if row else time.time(),
            last_seen_at=float(row.get("last_seen_at") or 0.0) if row else time.time(),
            message_count=int(row.get("message_count") or 0) if row else 0,
            digest=str(row.get("digest") or "") if row else "",
        )
