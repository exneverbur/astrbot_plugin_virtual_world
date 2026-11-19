"""VW 的流程型 skill（P4-2）：**一种"这类事按这几步做"的可复用方法**。

和经验（P3）的区别：经验是「做过一次，知道大概怎么做」；skill 是「已经验证过、
可以照着走的一串步骤，而且知道自己需要哪些能力」。2026-10 重写：**每个字段都要
进得了提示词才算数**，以前 `summary / when_not / verify / tags` 一个字都不进
提示词，等于白写。

* ``when_to_use``：什么时候该用（触发词，最短 2 个字——1 个字的触发词会误伤一切）；
* ``when_not``：什么时候**不该**用。命中即否决，比正向命中更硬；
* ``steps``：一步一步做什么，每步带 ``action``（对得上的动作/工具 id，没有就空串）；
* ``capabilities``：缺了就不能执行——**绝不假装可执行**；
* ``verify``：做完拿什么检查；
* ``status / version / hash / use_count / last_used_at``：能不能用、哪一版、用过几次；
* ``scope / source``：这条流程管多大范围、是谁写的（内置 / 派生 / Agent 提炼）。

内置 skill（official）与派生/Agent 提炼的（derived/agent）在数据上分开，
内置的**不允许被覆盖**——派生版本另存一条，源不同、身份不同。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

#: 这张表的形状版本。改了列/含义就 +1——启动时旧版本会先归档再重建（见 db.py）。
SCHEMA_VERSION = 2

STATUS_CANDIDATE = "candidate"
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
STATUSES = (STATUS_CANDIDATE, STATUS_ACTIVE, STATUS_REVOKED)

STATUS_LABELS = {
    STATUS_CANDIDATE: "候选",
    STATUS_ACTIVE: "可用",
    STATUS_REVOKED: "已撤销",
}

SOURCE_OFFICIAL = "official"
SOURCE_DERIVED = "derived"
SOURCE_AGENT = "agent"
SOURCES = (SOURCE_OFFICIAL, SOURCE_DERIVED, SOURCE_AGENT)

SOURCE_LABELS = {
    SOURCE_OFFICIAL: "内置",
    SOURCE_DERIVED: "派生副本",
    SOURCE_AGENT: "Agent 提炼",
}

DEFAULT_SCOPE = "general"

#: 触发词最短长度。1 个字的触发词（「查」「做」）会把什么都捞进来。
MIN_TRIGGER_CHARS = 2
#: 一次最多丢几条候选进提示词（以前是硬编码 [:3]，通用条会把专用条挤掉）。
MAX_CANDIDATES = 8
#: 候选块的总字数上限。超了从**末尾**整条丢，别把一句话砍成半句。
MAX_BLOCK_CHARS = 900
#: 空 situation（自主轮）时只列「名字 + 一句话」，最多几条。
IDLE_CANDIDATES = 3
#: 空 situation 的清单按会话节流（秒）。tick 是 5 秒一拍，不节流会把 token 吃干。
IDLE_INJECT_SECONDS = 600.0

HEADER = "【这类事可以照着做】"
IDLE_HEADER = "【这些事有现成做法】"


def _text(value: Any) -> str:
    return str(value or "").strip()


_TERM_SPLIT = re.compile(r"[/|、,;，；]+")


def _split_terms(value: Any) -> tuple[str, ...]:
    """把「a/b、c」这种写法拆成一个个词（触发词、能力、标签都用它）。

    传进来的是 JSON 解出来的列表也行：每一项再按分隔符拆一次，去空去重保序。
    """

    if isinstance(value, str):
        raw: list[str] = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = [str(item) for item in value]
    else:
        raw = []
    seen: set[str] = set()
    terms: list[str] = []
    for item in raw:
        for piece in _TERM_SPLIT.split(str(item or "")):
            piece = piece.strip()
            if piece and piece not in seen:
                seen.add(piece)
                terms.append(piece)
    return tuple(terms)


def _json_list(value: Any) -> list[Any]:
    """库里的 json 列 → 列表（容忍已经是列表、或者不是 json 的旧值）。"""

    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return [raw]
        if isinstance(parsed, list):
            return list(parsed)
        return [parsed]
    return []


@dataclass(frozen=True)
class Step:
    """一步：做什么（给人看）+ 用哪个动作/工具（给机器对）。"""

    text: str = ""
    action: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"text": self.text, "action": self.action}

    def __str__(self) -> str:  # noqa: D105 —— 拼 md / 拼提示词时当字符串用
        return self.text


def _step(value: Any) -> Step:
    if isinstance(value, Step):
        return value
    if isinstance(value, Mapping):
        return Step(
            text=_text(value.get("text") or value.get("step") or value.get("title")),
            action=_text(value.get("action") or value.get("tool")),
        )
    return Step(text=_text(value), action="")


def _steps(value: Any) -> tuple[Step, ...]:
    if isinstance(value, str):
        raw: list[Any] = [value]
    else:
        raw = list(value or ())
    out = tuple(step for step in (_step(item) for item in raw) if step.text)
    return out


def identity_of(title: Any, steps: Iterable[Any]) -> str:
    body = _text(title) + "\u0000" + "\u0000".join(_step(step).text for step in steps or ())
    return "skill_" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def content_hash(title: Any, steps: Iterable[Any], caps: Iterable[Any]) -> str:
    payload = {
        "title": _text(title),
        "steps": [_step(step).as_dict() for step in steps or ()],
        "caps": sorted(_text(item) for item in (caps or ())),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


@dataclass(frozen=True)
class Skill:
    """一个 skill（形状与库里的行一致；``file`` 只有内置才有）。"""

    skill_id: str = ""
    title: str = ""
    summary: str = ""
    when_to_use: tuple[str, ...] = ()
    when_not: str = ""
    steps: tuple[Step, ...] = ()
    capabilities: tuple[str, ...] = ()
    verify: str = ""
    tags: tuple[str, ...] = ()
    scope: str = DEFAULT_SCOPE
    source: str = SOURCE_AGENT
    version: int = 1
    hash: str = ""
    status: str = STATUS_CANDIDATE
    use_count: int = 0
    last_used_at: float = 0.0
    note: str = ""
    file: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def builtin(self) -> bool:
        return self.source == SOURCE_OFFICIAL

    @property
    def step_texts(self) -> tuple[str, ...]:
        return tuple(step.text for step in self.steps)

    def as_dict(self) -> dict[str, Any]:
        """库行（``vw_skills`` 的列；``file`` 不入库）。"""

        return {
            "skill_id": self.skill_id,
            "title": self.title,
            "summary": self.summary,
            "when_to_use": json.dumps(list(self.when_to_use), ensure_ascii=False),
            "when_not": self.when_not,
            "steps": json.dumps([step.as_dict() for step in self.steps], ensure_ascii=False),
            "capabilities": json.dumps(list(self.capabilities), ensure_ascii=False),
            "verify": self.verify,
            "tags": json.dumps(list(self.tags), ensure_ascii=False),
            "scope": self.scope,
            "source": self.source,
            "version": int(self.version),
            "hash": self.hash,
            "status": self.status,
            "use_count": int(self.use_count),
            "last_used_at": float(self.last_used_at),
            "note": self.note,
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "schema_version": SCHEMA_VERSION,
        }

    def view_dict(self) -> dict[str, Any]:
        """给前端/接口的形状（列表已是列表，步骤是 {text, action}）。"""

        return {
            "skill_id": self.skill_id,
            "title": self.title,
            "summary": self.summary,
            "when_to_use": list(self.when_to_use),
            "when_not": self.when_not,
            "steps": [step.as_dict() for step in self.steps],
            "capabilities": list(self.capabilities),
            "verify": self.verify,
            "tags": list(self.tags),
            "scope": self.scope,
            "source": self.source,
            "source_label": SOURCE_LABELS.get(self.source, self.source),
            "version": int(self.version),
            "hash": self.hash,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "use_count": int(self.use_count),
            "last_used_at": float(self.last_used_at),
            "note": self.note,
            "file": self.file,
            "builtin": self.builtin,
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
        }


def build(
    *,
    title: Any,
    when_to_use: Any = (),
    steps: Any,
    capabilities: Any = (),
    summary: Any = "",
    when_not: Any = "",
    verify: Any = "",
    tags: Any = (),
    scope: Any = "",
    source: str = SOURCE_AGENT,
    status: str = STATUS_CANDIDATE,
    note: Any = "",
    version: int = 1,
    use_count: int = 0,
    last_used_at: float = 0.0,
    file: Any = "",
    now: float = 0.0,
) -> Skill:
    """造一条 skill；没有标题或没有步骤 → 返回空 Skill（调用方按 ``skill_id`` 判）。"""

    name = _text(title)
    body = _steps(steps)
    if not name or not body:
        return Skill()
    triggers = _split_terms(when_to_use) or (name,)
    caps = _split_terms(capabilities)
    stamp = float(now or 0.0)
    return Skill(
        skill_id=identity_of(name, body),
        title=name,
        summary=_text(summary),
        when_to_use=triggers,
        when_not=_text(when_not),
        steps=body,
        capabilities=caps,
        verify=_text(verify),
        tags=_split_terms(tags),
        scope=_text(scope) or DEFAULT_SCOPE,
        source=_text(source) or SOURCE_AGENT,
        version=int(version or 1),
        hash=content_hash(name, body, caps),
        status=_text(status) or STATUS_CANDIDATE,
        use_count=int(use_count or 0),
        last_used_at=float(last_used_at or 0.0),
        note=_text(note),
        file=_text(file),
        created_at=stamp,
        updated_at=stamp,
    )


def from_row(row: Any) -> Skill | None:
    """库行 → Skill；读不动返回 None（旧行/坏 JSON 不该把整块提示词带崩）。"""

    if row is None:
        return None
    try:
        if isinstance(row, Mapping):
            data = dict(row)
        else:
            data = {key: row[key] for key in row.keys()}
    except Exception:
        return None
    if not data:
        return None
    try:
        title = _text(data.get("title"))
        steps = _steps(_json_list(data.get("steps")))
        if not title or not steps:
            return None
        caps = _split_terms(_json_list(data.get("capabilities")))
        return Skill(
            skill_id=_text(data.get("skill_id")) or identity_of(title, steps),
            title=title,
            summary=_text(data.get("summary")),
            when_to_use=_split_terms(_json_list(data.get("when_to_use"))) or (title,),
            when_not=_text(data.get("when_not")),
            steps=steps,
            capabilities=caps,
            verify=_text(data.get("verify")),
            tags=_split_terms(_json_list(data.get("tags"))),
            scope=_text(data.get("scope")) or DEFAULT_SCOPE,
            source=_text(data.get("source")) or SOURCE_AGENT,
            version=int(data.get("version") or 1),
            hash=_text(data.get("hash")),
            status=_text(data.get("status")) or STATUS_CANDIDATE,
            use_count=int(data.get("use_count") or 0),
            last_used_at=float(data.get("last_used_at") or 0.0),
            note=_text(data.get("note")),
            file=_text(data.get("file")),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 选择：when_not 否决 → 触发词命中长度 → 能力齐 → 用过多少次
# ---------------------------------------------------------------------------


def _veto(skill: Skill, situation: str) -> bool:
    """``when_not`` 里任何一个词出现在情境里 → 这条不能用。"""

    for piece in _split_terms(skill.when_not):
        if len(piece) >= MIN_TRIGGER_CHARS and piece in situation:
            return True
    return False


def match_length(skill: Skill, situation: Any) -> int:
    """命中长度（所有命中触发词的字数之和）；0 = 不命中，<0 不用（这里返回 0）。"""

    text = _text(situation)
    if len(text) < MIN_TRIGGER_CHARS or not skill.when_to_use:
        return 0
    if _veto(skill, text):
        return 0
    total = 0
    for trigger in skill.when_to_use:
        for piece in _split_terms(trigger):
            if len(piece) >= MIN_TRIGGER_CHARS and piece in text:
                total += len(piece)
    return total


def matches(skill: Skill, situation: Any) -> bool:
    """"这个情境该不该用这条"（只看正向 + 否决，不看能力）。"""

    return match_length(skill, situation) > 0


CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    # 能力名 ≠ 工具名：内置流程写的是「要一次搜索」，宿主里那件工具叫
    # `deepseek_web_search`（用户 m5221：内置两条一直显示"缺 web_search / agent_task"）。
    "web_search": ("web_search", "web_fetch", "search", "browse"),
    "agent_task": ("agent_task",),
}


def capability_present(capability: Any, available: Iterable[Any]) -> bool:
    """手上有没有这个能力：先按同名/词根认，再按别名认。

    认的规则（由窄到宽）：全名相同 → 名字尾段相同（`deepseek_web_search` 里的
    `web_search`）→ 别名命中同一段。**不用子串硬碰**，免得 `search` 把
    `research_writer` 之类也当上。
    """

    key = _text(capability)
    if not key:
        return True
    pool = {_text(item) for item in (available or ()) if _text(item)}
    if key in pool:
        return True
    for name in pool:
        segments = name.split("_")
        if name.endswith("_" + key) or key in segments:
            return True
    for alias in CAPABILITY_ALIASES.get(key, ()):
        for name in pool:
            if name == alias or alias in name.split("_"):
                return True
    return False


def missing_capabilities(skill: Skill, available: Iterable[Any]) -> tuple[str, ...]:
    """缺哪些能力（空 = 能执行）。**缺能力就不许假装能跑。**"""

    return tuple(
        cap for cap in skill.capabilities if not capability_present(cap, available)
    )


def usable(skill: Skill, *, available: Iterable[Any] = ()) -> bool:
    return skill.active and not missing_capabilities(skill, available)


def _rank_key(pair: tuple[Skill, int]) -> tuple[int, int, float, str]:
    skill, length = pair
    return (-length, -int(skill.use_count), -float(skill.updated_at), skill.title)


def candidates(
    skills: Iterable[Skill], situation: Any, available: Iterable[Any] = ()
) -> list[tuple[Skill, int]]:
    """按情境排好序的 (skill, 命中长度)；缺能力的不进候选。"""

    text = _text(situation)
    if not text:
        return []
    rows: list[tuple[Skill, int]] = []
    for skill in skills or ():
        if missing_capabilities(skill, available):
            continue
        length = match_length(skill, text)
        if length <= 0:
            continue
        rows.append((skill, length))
    rows.sort(key=_rank_key)
    return rows


def pick(
    skills: Iterable[Skill],
    situation: Any,
    available: Iterable[Any] = (),
    *,
    limit: int = MAX_CANDIDATES,
    idle_limit: int = IDLE_CANDIDATES,
) -> list[Skill]:
    """这一轮该把哪几条摆到她面前。

    * 有 situation：命中 + 能力齐的那些，最多 ``limit`` 条；
    * 空 situation（自主轮）：不挑，按 ``use_count`` 降序摆 ``idle_limit`` 条，
      好让她在没人说话的时候也记得自己会什么。
    """

    text = _text(situation)
    if not text:
        rows = [skill for skill in (skills or ()) if not missing_capabilities(skill, available)]
        rows.sort(key=lambda skill: (-int(skill.use_count), -float(skill.updated_at), skill.title))
        return rows[: max(0, int(idle_limit))]
    return [skill for skill, _ in candidates(skills, text, available)[: max(0, int(limit))]]


# ---------------------------------------------------------------------------
# 渲染：全部候选一行一条；只有第 1 名带步骤 + 做完检查
# ---------------------------------------------------------------------------


def _clip(value: Any, limit: int) -> str:
    text = _text(value)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def render_line(skill: Skill) -> str:
    """一行：``- 名字：一句话（什么时候用：…；什么时候不用：…）``。"""

    line = f"- {skill.title}"
    if skill.summary:
        line += f"：{_clip(skill.summary, 80)}"
    notes: list[str] = []
    if skill.when_to_use:
        notes.append("什么时候用：" + "、".join(skill.when_to_use[:3]))
    if skill.when_not:
        notes.append("什么时候不用：" + _clip(skill.when_not, 48))
    if notes:
        line += "（" + "；".join(notes) + "）"
    return line


def render_steps(skill: Skill) -> str:
    """第 1 名才带：``1. … / 2. … / 做完检查：…``。"""

    lines = [
        f"{index}. {step.text}" + (f"（{step.action}）" if step.action else "")
        for index, step in enumerate(skill.steps, 1)
    ]
    if not lines:
        return ""
    if skill.verify:
        lines.append("做完检查：" + skill.verify)
    return "\n".join(lines)


def prompt_block(
    picked: Iterable[Skill],
    *,
    available: Iterable[Any] = (),
    header: str = HEADER,
    with_steps: bool = True,
) -> str:
    """候选块。总字数 ≤ :data:`MAX_BLOCK_CHARS`，超了从末尾整条丢。

    ``with_steps=False``（空情境的"我会什么"清单）只出「名字 + 一句话」，不展开步骤。
    """

    skills = [skill for skill in (picked or ()) if skill and skill.title]
    if not skills:
        return ""
    budget = MAX_BLOCK_CHARS - len(header) - 1
    lines: list[str] = []
    for skill in skills:
        line = render_line(skill)
        if budget - (len(line) + 1) < 0:
            break
        lines.append(line)
        budget -= len(line) + 1
    if not lines:
        return ""
    first = skills[0]
    if with_steps and not missing_capabilities(first, available):
        for line in render_steps(first).split("\n"):
            if not line:
                continue
            if budget - (len(line) + 1) < 0:
                break
            lines.append(line)
            budget -= len(line) + 1
    return header + "\n" + "\n".join(lines)


def render(skill: Skill, *, include_steps: bool = True) -> str:
    """一条 skill 的完整渲染（详情页/测试用；提示词走 :func:`prompt_block`）。"""

    text = render_line(skill)
    if include_steps:
        steps = render_steps(skill)
        if steps:
            text += "\n" + steps
    return text


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


def promote(skill: Skill, *, now: float = 0.0) -> Skill:
    if skill.status == STATUS_REVOKED:
        return skill
    stamp = float(now or skill.updated_at or 0.0)
    return Skill(
        **{
            **skill.__dict__,
            "status": STATUS_ACTIVE,
            "version": skill.version + 1,
            "updated_at": stamp,
        }
    )


def revoke(skill: Skill, *, note: str = "", now: float = 0.0) -> Skill:
    stamp = float(now or skill.updated_at or 0.0)
    return Skill(
        **{
            **skill.__dict__,
            "status": STATUS_REVOKED,
            "note": _text(note) or skill.note,
            "version": skill.version + 1,
            "updated_at": stamp,
        }
    )


#: 内置 skill：稳定的方法，随插件发布，**不允许被派生版本覆盖**。
#:
#: 每条都必须有 summary / when_not / verify / steps（每步带 action），
#: 因为这些字段现在真的会进提示词（见 render_line / render_steps）。
BUILTIN_SKILLS: tuple[dict[str, Any], ...] = (
    {
        "title": "查清一件事再回答",
        "summary": "拿不准的事先查，别凭印象答；查完用自己的话讲，讲不出来就承认没查到。",
        "when_to_use": ("查一下", "搜一下", "是什么", "怎么回事"),
        "when_not": "闲聊、情绪安慰、你已经确定答案的小事——这种时候去查反而显得生分。",
        "steps": (
            {"text": "先确定要查什么（人/时间/方面）", "action": ""},
            {"text": "用搜索工具查一次", "action": "web_search"},
            {"text": "把结果用自己的话讲清楚，讲不出来就说没查到", "action": ""},
        ),
        "capabilities": ("web_search",),
        "verify": "回答里提到具体数字/日期/名字时，回头看一眼来源支不支持这个说法。",
        "tags": ("搜索", "核实"),
        "scope": "info",
        "note": "能力发现与信息查询的通用方法",
    },
    {
        "title": "缺信息先问一句",
        "summary": "信息不够就先问最关键的那一个，别自己编一个前提往下做。",
        "when_to_use": ("不太确定", "不知道", "帮我做"),
        "when_not": "对方明显在撒娇、发泄、随口一说——这种时候追问会扫兴。",
        "steps": (
            {"text": "先看自己手上有没有现成信息", "action": ""},
            {"text": "没有就问一句关键的那个问题", "action": ""},
            {"text": "只问一次，别追着问", "action": ""},
        ),
        "capabilities": (),
        "verify": "确认对方回答了你要的那个关键点，再动手。",
        "tags": ("澄清",),
        "scope": "talk",
        "note": "澄清方法",
    },
    {
        "title": "做一件要动手的事",
        "summary": "要真动手的事（写文件、查资料、跑代码）走执行器，别只在嘴上答应。",
        "when_to_use": ("帮我整理", "做个", "生成一份"),
        "when_not": "一句话就能答完的事，不必开任务。",
        "steps": (
            {"text": "先说清楚做成什么样", "action": ""},
            {"text": "一步一步做，每步留下结果", "action": "agent_task"},
            {"text": "做完把结果交回去，没做成要如实说", "action": ""},
        ),
        "capabilities": ("agent_task",),
        "verify": "任务回执是 succeeded，产物真的发到对方手上。",
        "tags": ("执行器",),
        "scope": "work",
        "note": "动作创建与执行方法",
    },
    {
        "title": "交出去之前自己验一遍",
        "summary": "发出去之前自己看一遍：数字对不对、文件开得开、有没有漏。",
        "when_to_use": ("发我", "交给我", "整理好"),
        "when_not": "随手分享一张图、一句吐槽——这种不用验收。",
        "steps": (
            {"text": "核对关键数字与来源", "action": ""},
            {"text": "确认没有把半成品交出去", "action": ""},
            {"text": "说清哪些没做到", "action": ""},
        ),
        "capabilities": (),
        "verify": "自己打开一次发给对方的东西，看起来正常再发。",
        "tags": ("验收",),
        "scope": "work",
        "note": "测试验收方法",
    },
    {
        "title": "出问题时先看证据",
        "summary": "先读真实错误，再决定重试、换路子还是如实说做不了；别猜原因、别反复重试同一个错。",
        "when_to_use": ("失败了", "没成", "报错"),
        "when_not": "对方只是在情绪上抱怨一句「烦死了」——先接情绪，不用立刻排查。",
        "steps": (
            {"text": "先看工具回执里的真实错误", "action": ""},
            {"text": "分清是没权限、没工具还是网络问题", "action": ""},
            {"text": "再决定重试还是换个办法", "action": ""},
        ),
        "capabilities": (),
        "verify": "你能说清这次失败的真实原因，而不是一句「出错了」。",
        "tags": ("诊断",),
        "scope": "work",
        "note": "故障诊断与维护方法",
    },
    {
        "title": "写一个插件给自己加本事",
        "summary": "要的是「以后一直在的能力」就走工坊：先拿骨架，再一文件一文件改，别一次糊一大坨。",
        "when_to_use": ("给你加个功能", "写个插件", "做个插件", "以后都能"),
        "when_not": "一次性的任务（查一下、写一份文件）不该写插件——写插件是给「以后每次都要用」的本事。",
        "steps": (
            {"text": "先把「要长期留着的能力」说成一句人话（记什么、查什么、提醒什么）", "action": ""},
            {"text": "调 vw_plugin_draft：先只给 name 和 tool_name，拿一份骨架", "action": "vw_plugin_draft"},
            {"text": "一次改一个文件（main.py 里的工具函数、metadata.yaml 的说明）", "action": "vw_plugin_draft"},
            {"text": "改完调 vw_plugin_draft 再验证一遍（装之前先过校验）", "action": "vw_plugin_draft"},
        ),
        "capabilities": ("vw_plugin_draft",),
        "verify": "草稿里 main.py + metadata.yaml 都在，校验通过；工具名与动作名是她看得懂的人话。",
        "tags": ("插件", "工坊"),
        "scope": "build",
        "note": "插件工坊：写草稿",
    },
    {
        "title": "把插件装上并真的用起来",
        "summary": "装完不算完，要用一次证明它真能干活；装不上就如实说卡在哪一步。",
        "when_to_use": ("装上", "装好", "能用了"),
        "when_not": "草稿还没校验通过时别装——装进去也是坏的。",
        "steps": (
            {"text": "调 vw_plugin_install（slug 用草稿的目录名）", "action": "vw_plugin_install"},
            {"text": "看回执里的动作 id 与工具名（ext_xxx / vw_xxx）", "action": ""},
            {"text": "立刻用那个工具办一件小事，证明真的能用", "action": ""},
            {"text": "回报主人：装上了、试过了、结果是什么", "action": ""},
        ),
        "capabilities": ("vw_plugin_install",),
        "verify": "工具调用回执 ok，且返回的是这次真正办成的结果。",
        "tags": ("插件", "安装"),
        "scope": "build",
        "note": "插件工坊：安装与验收",
    },
    {
        "title": "把一次经验凝练成标准流程",
        "summary": "一件真做成过的事，拆成下次能照着走的三五步，再存成流程。",
        "when_to_use": ("学会了", "以后这样", "记个规矩"),
        "when_not": "随口聊天、一次性的小事、还没做成就想总结——这些先别写成流程。",
        "steps": (
            {"text": "翻出那一次的真实经过（哪次、用了什么、结果如何）", "action": "vw_experience_lookup"},
            {"text": "把方法拆成 3~5 步，一步只做一件事", "action": ""},
            {"text": "每一步写清用哪个动作或工具；对不上就说明这步还不算方法", "action": ""},
            {"text": "补上什么时候用、什么时候不用、做完怎么检查", "action": ""},
            {"text": "拿一个真实情境对一遍触发词，对不中就改，别硬存", "action": ""},
        ),
        "capabilities": (),
        "verify": "新流程里每个动词都能对上一个已注册的动作或工具 id；有一个对不上就还没凝练好。",
        "tags": ("流程", "复盘"),
        "scope": "meta",
        "note": "元流程：把经验写成流程",
    },
    {
        "title": "给世界加内容",
        "summary": "要加东西，先看清现有的，再一次只加一个，然后让她自己走一遍。",
        "when_to_use": ("加个地方", "加个动作", "剧情"),
        "when_not": "只是问一句这个动作怎么用、或者临时改一句台词——不用加新东西。",
        "steps": (
            {"text": "先看现有的节点和动作，别重复造一个已经有了的", "action": ""},
            {"text": "一次只加一个动作或一个节点，加完立刻保存", "action": ""},
            {"text": "reload 一次，确认配置读进来了、没报错", "action": ""},
            {"text": "让她在那个地点走一遍这个动作，看回执对不对", "action": ""},
        ),
        "capabilities": (),
        "verify": "新动作有她能看懂的一句说明；新节点在配置里有边连着，走得过去。",
        "tags": ("世界", "增改"),
        "scope": "world",
        "note": "世界内容的增改流程",
    },
    {
        "title": "查过的事要留下",
        "summary": "查完别只回一句就走：把结论摘成一句留在知识里，下次先翻它再查。",
        "when_to_use": ("查完", "记下来"),
        "when_not": "没查到、或者只是随手扫了一眼——别把没把握的东西写进知识。",
        "steps": (
            {"text": "查完先写结论：一句话，说清是什么", "action": ""},
            {"text": "从工具回执里挑一句原文当摘要，别自己改写", "action": "web_search"},
            {"text": "存成一条知识卡（关键数字/日期/名字要留着）", "action": ""},
            {"text": "下次遇到同一件事，先翻这张卡，别再查一遍", "action": ""},
        ),
        "capabilities": ("web_search",),
        "verify": "摘要能在工具回执里原样找到（是子串），不是你脑补的说法。",
        "tags": ("知识", "留档"),
        "scope": "info",
        "note": "查询结果的沉淀方法",
    },
    {
        "title": "改设置前先备份",
        "summary": "动设置之前先复制一份，改完要能重新启动；不对劲就拿备份盖回去。",
        "when_to_use": ("改一下设置", "关了"),
        "when_not": "只是看一眼当前设置、或者改一个显示开关——不用备份。",
        "steps": (
            {"text": "先找到要改的那个配置文件/那一项", "action": ""},
            {"text": "复制一份带日期的备份（.bak 或 .json）", "action": ""},
            {"text": "只改这一处，别顺手改别的", "action": ""},
            {"text": "重启或重新加载一次，确认还能起来、这一项真的变了", "action": ""},
            {"text": "起不来就立刻用备份盖回去，再说清哪一步出的问题", "action": ""},
        ),
        "capabilities": (),
        "verify": "有备份文件，且改完能正常启动、那一项确实按预期变了。",
        "tags": ("维护", "备份"),
        "scope": "maintain",
        "note": "改配置的保守流程",
    },
    {
        "title": "给扩展加能力（不是写插件）",
        "summary": "要挂进自己的流程里就写扩展；只在需要长期对外的工具时才写插件。",
        "when_to_use": ("扩展", "钩子", "on_tick"),
        "when_not": "要的是一个能被别的插件调用的工具——那是插件，不是扩展。",
        "steps": (
            {"text": "先判断：这是挂进自己的提示词/回合，还是要一个对外工具", "action": ""},
            {"text": "写扩展声明：钩子点（on_tick / 提示词层）与它给的值", "action": ""},
            {"text": "跑一次契约测试，确认声明与引擎要的形状一致", "action": ""},
            {"text": "装上，让她走一个回合，看她真的看得见扩展给的那几个数", "action": ""},
        ),
        "capabilities": (),
        "verify": "契约测试通过，且这一轮的提示词里真的出现了扩展给的值。",
        "tags": ("扩展", "契约"),
        "scope": "build",
        "note": "扩展与插件的分界",
    },
)


# ---------------------------------------------------------------------------
# 文件层：skills/*.md（内置以文件为准，改了文件就改了注入）
# ---------------------------------------------------------------------------

FILE_SUFFIX = ".md"

#: 「## 小节名」→ 字段名。用户改文件时按这套小标题走，顺序随便。
SECTION_KEYS = {
    "什么时候用": "when_to_use",
    "触发": "when_to_use",
    "需要的能力": "capabilities",
    "能力": "capabilities",
    "一句话": "summary",
    "什么时候不用": "when_not",
    "步骤": "steps",
    "做完检查": "verify",
    "验收": "verify",
    "标签": "tags",
    "范围": "scope",
}

LIST_FIELDS = ("when_to_use", "capabilities", "steps", "tags")

SKILL_FILE_HEADER = "<!-- 这一条是内置流程，可以直接改；改坏了点「恢复内置」会还原成默认。 -->"

#: 步骤行的「文字 → 动作」分隔符；没有就不写。
STEP_ACTION_SEP = " → "


def skill_file_text(item: Mapping[str, Any]) -> str:
    """把一条 skill 写成给人看、也给人改的 Markdown。

    步骤写成 ``1. 做什么 → 动作id``：右边是给机器对的，左边是给人看的。
    """

    lines = ["# " + _text(item.get("title")), "", SKILL_FILE_HEADER]

    def block(name: str, key: str, *, numbered: bool = False) -> None:
        values = item.get(key) or ()
        if isinstance(values, str):
            values = (values,) if values.strip() else ()
        usable = [_text(value) for value in values if _text(value)]
        if not usable:
            return
        lines.extend(["", "## " + name])
        for index, value in enumerate(usable, 1):
            lines.append(f"{index}. {value}" if numbered else "- " + value)

    block("什么时候用", "when_to_use")
    block("需要的能力", "capabilities")
    summary = _text(item.get("summary"))
    if summary:
        lines.extend(["", "## 一句话", summary])
    when_not = _text(item.get("when_not"))
    if when_not:
        lines.extend(["", "## 什么时候不用", when_not])
    steps = _steps(item.get("steps"))
    if steps:
        lines.extend(["", "## 步骤"])
        for index, step in enumerate(steps, 1):
            body = step.text + (STEP_ACTION_SEP + step.action if step.action else "")
            lines.append(f"{index}. {body}")
    verify = _text(item.get("verify"))
    if verify:
        lines.extend(["", "## 做完检查", verify])
    block("标签", "tags")
    scope = _text(item.get("scope"))
    if scope:
        lines.extend(["", "## 范围", scope])
    return chr(10).join(lines).rstrip() + chr(10)


def _unbullet(value: str) -> str:
    text = value.strip()
    if text.startswith("- "):
        return text[2:].strip()
    head, _, rest = text.partition(". ")
    if head.isdigit() and rest:
        return rest.strip()
    return text


def parse_skill_file(text: str) -> dict[str, Any] | None:
    """读回一份 skill；没有标题或没有步骤就当无效（返回 None）。"""

    title = ""
    current = ""
    buckets: dict[str, list[str]] = {}
    body = str(text or "").replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))
    for raw in body.split(chr(10)):
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue
        if line.startswith("## "):
            current = SECTION_KEYS.get(line[3:].strip(), "")
            if current:
                buckets.setdefault(current, [])
            continue
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if not current:
            continue
        value = _unbullet(line)
        if value:
            buckets.setdefault(current, []).append(value)
    if not title:
        return None
    item: dict[str, Any] = {"title": title}
    for key, values in buckets.items():
        if key == "steps":
            parsed: list[Step] = []
            for value in values:
                head, sep, action = value.partition(STEP_ACTION_SEP)
                parsed.append(Step(text=head.strip(), action=action.strip() if sep else ""))
            item["steps"] = tuple(parsed)
        elif key in LIST_FIELDS:
            item[key] = tuple(values)
        else:
            item[key] = " ".join(values).strip()
    if not item.get("steps"):
        return None
    return item


def default_skill_files() -> tuple[tuple[str, str], ...]:
    """默认的几份文件（文件名固定，方便「恢复内置」按名字写回）。"""

    return tuple(
        (f"skill_{index:02d}{FILE_SUFFIX}", skill_file_text(item))
        for index, item in enumerate(BUILTIN_SKILLS, 1)
    )


def builtin_skills(*, now: float = 0.0) -> list[Skill]:
    """内置 skill 列表（source=official、status=active、不可被覆盖）。"""

    rows: list[Skill] = []
    for item in BUILTIN_SKILLS:
        skill = build(
            title=item.get("title"),
            when_to_use=item.get("when_to_use"),
            steps=item.get("steps"),
            capabilities=item.get("capabilities"),
            summary=item.get("summary", ""),
            when_not=item.get("when_not", ""),
            verify=item.get("verify", ""),
            tags=item.get("tags", ()),
            scope=item.get("scope", ""),
            source=SOURCE_OFFICIAL,
            status=STATUS_ACTIVE,
            note=item.get("note", ""),
            file=item.get("file", ""),
            now=now,
        )
        if skill.skill_id:
            rows.append(skill)
    return rows
