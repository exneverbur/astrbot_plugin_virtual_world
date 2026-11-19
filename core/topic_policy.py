"""Versioned, fail-closed topic permissions. Pure logic; persist in visibility_json.

The caller supplies authoritative scope membership, source rows and question evidence.
Model output must never construct PolicyTarget or RequestEvidence.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Iterable
from dataclasses import dataclass
from typing import Any

POLICY_VERSION = 1
_LEVELS = {'normal': 0, 'person': 1, 'private': 2, 'locked': 3}
_TRUSTED = {'explicit', 'trusted_rule'}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ''


def _ids(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return sorted({_text(v) for v in value if _text(v)})


def _data(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping) or callable(getattr(value, 'keys', None)):
        try:
            return value[name]
        except (KeyError, IndexError, TypeError):
            return default
    return getattr(value, name, default)


def _level(value: Any) -> str:
    text = _text(value)
    return text if text in _LEVELS else 'private'


def _revision(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


def build_policy(*, scope_id: str, origin_session_id: str,
                 sensitivity: str = 'private', subject_ids: Iterable[str] = (),
                 allowed_sessions: Mapping[str, Any] | None = None,
                 hard_private: bool = False, authority: str = 'trusted_rule',
                 policy_revision: int = 1) -> dict[str, Any]:
    """Trusted writer API. None = unrestricted; restricted [] always denies."""
    hard = hard_private is True or sensitivity == 'locked'
    level = 'private' if hard else _level(sensitivity)
    sessions = {'mode': 'unrestricted', 'ids': []} if allowed_sessions is None else dict(allowed_sessions)
    policy = {'policy_version': POLICY_VERSION, 'scope_id': _text(scope_id),
              'origin_session_id': _text(origin_session_id), 'sensitivity': level,
              'subject_ids': _ids(subject_ids), 'allowed_sessions': sessions,
              'hard_private': hard, 'authority': authority,
              'policy_revision': _revision(policy_revision)}
    return normalize_policy(policy)


def normalize_policy(raw: Any, *, scope_id: str = '', origin_session_id: str = '') -> dict[str, Any]:
    """Read policy; old/malformed declarations are legacy_unknown, never public.

    Row scope/origin, when supplied, must match the policy. Legacy may only be
    queried in a proven exact origin. Missing origin is never guessed from scope.
    """
    data = _data(raw)
    damaged = (isinstance(raw, str) and bool(raw.strip()) and not data) or (raw is not None and not isinstance(raw, (str, Mapping)))
    scope = _text(data.get('scope_id')) or _text(data.get('scope')) or _text(scope_id)
    origin = _text(data.get('origin_session_id')) or _text(origin_session_id)
    sessions = data.get('allowed_sessions')
    valid_sessions = (isinstance(sessions, Mapping) and
                      sessions.get('mode') in ('unrestricted', 'restricted') and
                      isinstance(sessions.get('ids'), (list, tuple)) and
                      all(isinstance(x, str) and x.strip() for x in sessions['ids']))
    valid = (type(data.get('policy_version')) is int and data.get('policy_version') == POLICY_VERSION and
             data.get('authority') in (_TRUSTED | {'legacy_unknown'}) and valid_sessions and
             data.get('sensitivity') in ('normal', 'person', 'private', 'locked') and
             isinstance(data.get('subject_ids'), (list, tuple)) and
             all(isinstance(x, str) and x.strip() for x in data['subject_ids']) and
             type(data.get('hard_private')) is bool and
             ((bool(scope) and bool(origin)) or isinstance(data.get('source_policies'), (list, tuple)) and bool(data['source_policies'])) and
             type(data.get('policy_revision')) is int and data['policy_revision'] > 0)
    conflict = ((_text(scope_id) and scope != _text(scope_id)) or
                (_text(origin_session_id) and origin != _text(origin_session_id)))
    hard = data.get('hard_private') is True or data.get('locked') is True or data.get('lock') is True or data.get('sensitivity') == 'locked'
    if data.get('share') is False or data.get('deny') is True:
        damaged = True
    level = _level(data.get('sensitivity'))
    if data.get('private') is True or data.get('audience') in ('private', 'self'):
        level = 'private'
    if hard:
        level = 'private'
    if valid_sessions:
        restriction = {'mode': sessions['mode'], 'ids': _ids(sessions['ids'])}
    elif 'sessions' in data:
        # Legacy explicit [] remains denial, rather than absence of restriction.
        restriction = {'mode': 'restricted', 'ids': _ids(data['sessions'])}
    else:
        restriction = {'mode': 'unrestricted', 'ids': []}
    result = {'policy_version': POLICY_VERSION, 'scope_id': scope, 'origin_session_id': origin,
              'sensitivity': level if valid else 'private',
              'subject_ids': _ids(data.get('subject_ids', data.get('persons', []))),
              'allowed_sessions': restriction, 'hard_private': hard,
              'authority': data.get('authority') if valid and not conflict else 'legacy_unknown',
              'policy_revision': _revision(data.get('policy_revision'))}
    if conflict or damaged or data.get('deny_all') is True or ('policy_version' in data and not valid and 'source_policies' not in data):
        result['deny_all'] = True
        result['origin_session_id'] = ''
        result['allowed_sessions'] = {'mode': 'restricted', 'ids': []}
    # Never collapse independent origins/legacy states into a single first-origin policy.
    if 'source_policies' in data:
        clauses = data['source_policies']
        result['source_policies'] = ([normalize_policy(p) for p in clauses]
                                     if isinstance(clauses, (list, tuple)) and clauses
                                     else [normalize_policy({})])
    return result


def _constrained_clauses(policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Propagate aggregate additions without redistributing inherited maxima.

    A maximum person/private label and union of subjects summarize the clauses.
    Only a stricter aggregate level or newly added subjects are extra constraints.
    Each constraint retains the exact origin of its own source clause.
    """
    clauses = policy['source_policies']
    inherited_level = max((p['sensitivity'] for p in clauses), key=_LEVELS.get)
    inherited_subjects = set().union(*(set(p['subject_ids']) for p in clauses))
    added_subjects = set(policy['subject_ids']) - inherited_subjects
    stricter = _LEVELS[policy['sensitivity']] > _LEVELS[inherited_level]
    result = []
    for clause in clauses:
        constrained = dict(clause)
        if policy['hard_private']:
            constrained['hard_private'] = True
        if stricter:
            constrained['sensitivity'] = policy['sensitivity']
        if added_subjects:
            constrained['subject_ids'] = sorted(set(clause['subject_ids']) | added_subjects)
            constrained['sensitivity'] = max((constrained['sensitivity'], 'person'), key=_LEVELS.get)
        if policy['allowed_sessions']['mode'] == 'restricted':
            ids = set(policy['allowed_sessions']['ids'])
            if clause['allowed_sessions']['mode'] == 'restricted':
                ids &= set(clause['allowed_sessions']['ids'])
            constrained['allowed_sessions'] = {'mode': 'restricted', 'ids': sorted(ids)}
        result.append(constrained)
    return result


def merge_policies(*policies: Any) -> dict[str, Any]:
    """AND of sources, retained as individual clauses; hard locks combine by OR."""
    normalized = []
    seen = set()

    def add(raw: Any) -> None:
        policy = normalize_policy(raw)
        clauses = policy.get('source_policies')
        if clauses and not policy.get('deny_all'):
            for constrained in _constrained_clauses(policy):
                add(constrained)
            return
        key = json.dumps(policy, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            normalized.append(policy)

    for raw in policies:
        add(raw)
    if not normalized:
        normalized = [normalize_policy({})]
    first = normalized[0]
    scopes = {p['scope_id'] for p in normalized}
    origins = {p['origin_session_id'] for p in normalized}
    restricted = [set(p['allowed_sessions']['ids']) for p in normalized
                  if p['allowed_sessions']['mode'] == 'restricted']
    sessions = set.intersection(*restricted) if restricted else set()
    result = build_policy(scope_id=first['scope_id'] if len(scopes) == 1 else '',
                          origin_session_id=first['origin_session_id'],
                          sensitivity=max((p['sensitivity'] for p in normalized), key=_LEVELS.get),
                          subject_ids=set().union(*(set(p['subject_ids']) for p in normalized)),
                          allowed_sessions={'mode': 'restricted' if restricted else 'unrestricted', 'ids': sorted(sessions)},
                          hard_private=any(p['hard_private'] for p in normalized),
                          authority='trusted_rule' if all(p['authority'] in _TRUSTED for p in normalized) else 'legacy_unknown',
                          policy_revision=max(p['policy_revision'] for p in normalized))
    # Composite metadata need not have a common origin; clauses stay exact.
    result.pop('deny_all', None)
    result['origin_session_id'] = first['origin_session_id'] if len(origins) == 1 else ''
    result['source_policies'] = normalized
    return result


def tighten_policy(base: Any, suggestion: Any) -> dict[str, Any]:
    """Untrusted suggestion can add subjects/restrictions/locks, never grant access."""
    result = normalize_policy(base)
    data = _data(suggestion)
    previous = dict(result)
    suggested = _level(data.get('sensitivity'))
    if data.get('private') is True or data.get('audience') in ('private', 'self'):
        suggested = 'private'
    result['sensitivity'] = max((result['sensitivity'], suggested), key=_LEVELS.get)
    result['subject_ids'] = sorted(set(result['subject_ids']) | set(_ids(data.get('subject_ids', data.get('persons', [])))))
    result['hard_private'] = result['hard_private'] or data.get('hard_private') is True or data.get('locked') is True or data.get('lock') is True or data.get('sensitivity') == 'locked'
    if result['hard_private']:
        result['sensitivity'] = 'private'
    incoming = data.get('allowed_sessions')
    if incoming is None and 'sessions' in data:
        incoming = {'mode': 'restricted', 'ids': data['sessions']}
    if incoming is not None:
        if not isinstance(incoming, Mapping) or incoming.get('mode') not in ('unrestricted', 'restricted') or not isinstance(incoming.get('ids'), (list, tuple)):
            incoming = {'mode': 'restricted', 'ids': []}
        if incoming['mode'] == 'restricted':
            ids = set(_ids(incoming['ids']))
            if result['allowed_sessions']['mode'] == 'restricted':
                ids &= set(result['allowed_sessions']['ids'])
            result['allowed_sessions'] = {'mode': 'restricted', 'ids': sorted(ids)}
    if 'source_policies' in result:
        result['source_policies'] = [tighten_policy(p, data) for p in result['source_policies']]
    if result != previous:
        result['policy_revision'] += 1
    return result


@dataclass(frozen=True)
class PolicyTarget:
    scope_id: str
    session_id: str
    member_ids: frozenset[str] = frozenset()
    membership_verified: bool = False


@dataclass(frozen=True)
class RequestEvidence:
    session_id: str
    event_id: str
    explicit_question: bool = False
    source_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


def _check(policy: dict[str, Any], target: PolicyTarget, usage: str, evidence_ok: bool) -> PolicyDecision:
    if policy.get('deny_all') is True:
        return PolicyDecision(False, 'damaged or conflicting policy')
    clauses = policy.get('source_policies')
    if clauses is not None:
        for constrained in _constrained_clauses(policy):
            decision = _check(constrained, target, usage, evidence_ok)
            if not decision:
                return decision
        # Aggregation metadata may have no common origin; independent clauses
        # carry authority. Their intersection is the complete authorization.
        return PolicyDecision(True, 'all source clauses permit')
    if not target.scope_id or policy['scope_id'] != target.scope_id:
        return PolicyDecision(False, 'scope mismatch or unknown')
    origin = policy['origin_session_id']
    if not origin or not target.session_id:
        return PolicyDecision(False, 'exact origin or target unknown')
    allowed = policy['allowed_sessions']
    if allowed['mode'] == 'restricted' and target.session_id not in allowed['ids']:
        return PolicyDecision(False, 'restricted session denied')
    if policy['authority'] not in _TRUSTED or policy['hard_private'] or policy['sensitivity'] == 'private':
        if usage not in ('answer', 'reference') or not evidence_ok or target.session_id != origin:
            return PolicyDecision(False, 'private/legacy requires explicit question in exact origin')
    elif policy['sensitivity'] == 'person' and target.session_id != origin:
        subjects = set(policy['subject_ids'])
        # Cross-origin needs an explicit session grant AND every known subject.
        if allowed['mode'] != 'restricted' or not subjects or not target.membership_verified or not subjects <= set(target.member_ids):
            return PolicyDecision(False, 'person requires explicit grant and all verified subjects')
    return PolicyDecision(True, 'policy permits')


def source_is_withdrawn(source: Any) -> bool:
    """SQLite deleted_at: None/finite numeric zero active; everything else denies."""
    value = _get(source, 'deleted_at', None)
    if value is not None:
        import math
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != 0:
            return True
    return _get(source, 'status', 'available') not in ('available', 'active')


def authorize(topic: Any, source_policies: Any, target: PolicyTarget, usage: str,
              request_evidence: RequestEvidence | None = None, *, now: float | None = None) -> PolicyDecision:
    """Validate all exact source identities, topic scope and policy before use.

    Caller must verify target.session_id currently belongs to target.scope_id.
    Evidence must be derived from user input, bound to the cited sources.
    """
    if not isinstance(target, PolicyTarget) or usage not in ('introduce', 'progress', 'outcome', 'answer', 'reference'):
        return PolicyDecision(False, 'invalid target or usage')
    if _get(topic, 'scope_id', '') != target.scope_id:
        return PolicyDecision(False, 'topic scope mismatch')
    if _get(topic, 'status', 'available') != 'available':
        return PolicyDecision(False, 'topic unavailable')
    def expired(value: Any) -> bool:
        if value is None:
            return False
        try:
            import math
            return now is None or not math.isfinite(float(value)) or float(value) <= float(now)
        except (ValueError, TypeError):
            return True

    if expired(_get(topic, 'expires_at', None)):
        return PolicyDecision(False, 'topic expiry unknown or expired')
    ids = _ids(_get(topic, 'source_event_ids', ()))
    if not ids:
        return PolicyDecision(False, 'missing source identities')
    if not isinstance(source_policies, Mapping):
        return PolicyDecision(False, 'source lookup unavailable')
    evidence_ok = (isinstance(request_evidence, RequestEvidence) and
                   request_evidence.explicit_question is True and bool(request_evidence.event_id) and
                   request_evidence.session_id == target.session_id and
                   set(ids) <= set(request_evidence.source_event_ids))
    for source_id in ids:
        source = source_policies.get(source_id)
        if source is None:
            return PolicyDecision(False, 'missing source: ' + source_id)
        if source_is_withdrawn(source):
            return PolicyDecision(False, 'source withdrawn or unknown status')
        if expired(_get(source, 'expires_at', None)):
            return PolicyDecision(False, 'source expiry unknown or expired')
        row_id = _get(source, 'event_id', source_id)
        if row_id != source_id:
            return PolicyDecision(False, 'source identity mismatch')
        visibility = _get(source, 'visibility', _get(source, 'visibility_json', source))
        policy = normalize_policy(visibility, scope_id=_get(source, 'scope_id', ''),
                                  origin_session_id=_get(source, 'origin_session_id', ''))
        decision = _check(policy, target, usage, bool(evidence_ok))
        if not decision:
            return decision
    details = _get(topic, 'details', {})
    if isinstance(details, Mapping) and 'visibility' in details:
        return _check(normalize_policy(details['visibility']), target, usage, bool(evidence_ok))
    return PolicyDecision(True, 'all sources permit')


# ---------------- T8-2/T8-3 谈资质量闸（纯逻辑；不碰上面的授权） ----------------
#
# 用户 2026-10-23 定的口径：能进谈资池的只有 ``event`` / ``chat``（私聊群聊都算）/
# ``activity`` 里"她亲手做了具体事"；``dream`` / ``manual`` 不进谈资。
# 这里只放**判定**，写库与召回在引擎侧；判定必须是纯函数，测试直接用假数据喂。

TALK_SOURCE_KINDS = ('event', 'chat', 'activity')
"""能当谈资的来源类型（其余一律不进谈资池）。"""

TALK_TEXT_LIMIT = 40
"""一条谈资 / 一句 ``share`` 的字数上限（口径：≤40 字）。"""

TALK_MIN_IMPORTANCE = 0.35
"""活人经历做成谈资的最低重要度（口径：≥0.35）。"""

REFERENCE_MIN_IMPORTANCE = 0.3
"""reference 召回的最低重要度（口径：≥0.3）。"""

REFERENCE_MIN_SIMILARITY = 0.35
"""reference 召回的最低余弦相似度；低于它这一轮不许注入 reference。"""

TALK_COOLDOWN_SECONDS = 7 * 86400.0
"""同一条谈资两次外发之间至少隔 7 天（口径：7 天冷却）。"""

HEARD_IN_GROUP = 'group'
HEARD_IN_PRIVATE = 'private'

_TOOL_ACTIONS = (
    '查一下', '搜一下', '看一眼', '打开', '刷新', '截图', '翻译', '设个提醒',
    '调出', '点开', '滑一下', '切到', '输入', '复制', '粘贴', '保存', '上传',
)
"""工具/屏幕动作：不算"她亲手做了具体事"，不进谈资。"""

_ACTION_WORDS = (
    '吃', '喝', '买', '卖', '做', '去', '来', '走', '跑', '搬', '养', '种', '洗',
    '煮', '烤', '炒', '切', '修', '换', '装', '拆', '剪', '画', '写', '拍', '唱',
    '弹', '跳', '骑', '读', '看', '听', '学', '教', '问', '答', '说', '聊', '见',
    '找', '找', '拿到', '收到', '寄', '送', '借', '还', '丢', '忘', '摔', '撞',
    '病', '疼', '睡', '醒', '起', '迟到', '加班', '请假', '考试', '面试', '搬家',
    '旅行', '回家', '出差', '结婚', '分手', '吵架', '和好', '道歉', '答应', '拒绝',
    '开', '关', '发', '收', '改', '签', '交', '领', '捡', '擦', '拖', '晾', '叠',
    # 结果类：不一定有"做事"的动词，但确有其事（猫把花盆打翻了、杯子碎了）
    '打', '翻', '倒', '弄', '坏', '破', '碎', '掉', '堵', '漏', '涨', '跌',
    '赢', '输', '考', '升', '降', '办', '参加', '报名', '放假', '开学', '开业',
)
"""具体动作/结果的字眼表（启发式；宁可少收一条，也不收一句空话）。"""

_NARRATION_VERBS = ('说', '讲', '告诉', '表示', '透露', '据说', '听说')
"""转述类动词：一段话里它出现在动作之前，就是在替别人叙述（口径：不是第三人称叙述）。"""

_STOP_CHARS = frozenset(
    '的了吗呢吧啊呀哦嗯我你他她它们这那哪就有在不也都还又很太没别个和跟把被给对让说想要会能可要过请喂哈吧啦嘛'
)
"""抽钩子词时要剥掉的字（虚词/代词），剥不掉就不算具体词。"""

_HOOK_STOP_WORDS = frozenset(
    ('今天', '昨天', '明天', '现在', '刚才', '刚刚', '后来', '然后', '真的', '其实',
     '可能', '大概', '好像', '觉得', '事情', '东西', '时候', '什么', '怎么', '这样')
)
"""当钩子太虚的词：时间、语气、泛称，接不住具体话题。"""


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    import math
    return number if math.isfinite(number) else default


def talk_source_ok(source_kind: Any) -> bool:
    """这种来源能不能进谈资（``dream`` / ``manual`` / ``tool`` / ``media`` 都不能）。"""

    return _text(source_kind) in TALK_SOURCE_KINDS


def talk_activity_ok(text: Any) -> bool:
    """``activity`` 里只收"她亲手做了具体事"；查手机、截图这类工具动作不收。"""

    body = _text(text) if isinstance(text, str) else ''
    body = ' '.join(body.split())
    if not body:
        return False
    return not any(word in body for word in _TOOL_ACTIONS)


def talk_text_reason(text: Any, *, limit: int = TALK_TEXT_LIMIT) -> str:
    """谈资正文的质量闸：返回 ``''`` 表示通过，否则是一句拒绝理由。

    口径：必须有具体动作或结果、不是第三人称叙述、有可讲来源。
    "摘要不得直接当 fact"由调用侧保证（写 facts 时用原话，不是 summary）。
    """

    body = ' '.join(str(text or '').split())
    if not body:
        return '空的'
    if len(body) > max(1, int(limit)):
        return '太长'
    if len(body) < 6:
        return '太短，说不出具体的事'
    if body[0] in ('她', '他', '它'):
        return '第三人称叙述'
    if body.startswith(('对方', '有人', '某人', '人家')):
        return '第三人称叙述'
    if not body.startswith('我'):
        marks = [body.find(word) for word in _NARRATION_VERBS]
        marks = [mark for mark in marks if 0 <= mark <= 10]
        if marks:
            return '第三人称叙述'
    if any(body.startswith(prefix) for prefix in ('好像', '可能', '大概')):
        return '没有具体动作或结果'
    if not any(word in body for word in _ACTION_WORDS) and not any(ch.isdigit() for ch in body):
        return '没有具体动作或结果'
    return ''


def talk_source_label(source_kind: Any, *, heard_in: str = '', speaker: str = '') -> str:
    """注入时必须带的来源标记（群里听来的 / 私聊 / 事件）+ 说话人名字。"""

    kind = _text(source_kind) if isinstance(source_kind, str) else ''
    who = ' '.join(str(speaker or '').split())[:12]
    if kind == 'event':
        return '（事件）'
    if kind == 'activity':
        return '（她自己做的）'
    where = {HEARD_IN_GROUP: '群里听来的', HEARD_IN_PRIVATE: '私聊'}.get(_text(heard_in), '聊天里听来的')
    return f'（{where}，{who}说的）' if who else f'（{where}）'


def talk_text_key(text: Any) -> str:
    """去重用的归一文本：去掉空白与常见标点，英文小写。"""

    punctuation = frozenset(' \t\r\n，。！？、；：""\'\'（）()【】[]{}<>…—－-.,!?;:"\'`~·/\\|')
    return ''.join(
        ch for ch in str(text or '').lower() if ch not in punctuation
    )


def talk_cooldown_active(last_at: Any, now: Any, *, seconds: float = TALK_COOLDOWN_SECONDS) -> bool:
    """同一 topic 的外发冷却是否还没过（``last_at`` 为 0/无效 = 没说过）。"""

    stamp = _number(last_at, 0.0)
    if stamp <= 0:
        return False
    elapsed = _number(now, 0.0) - stamp
    return 0.0 <= elapsed < max(0.0, _number(seconds, 0.0))


def reference_ok(importance: Any, similarity: Any) -> bool:
    """reference 的硬前置：重要度与相似度都过线才允许注入。"""

    return (_number(importance) >= REFERENCE_MIN_IMPORTANCE
            and _number(similarity) >= REFERENCE_MIN_SIMILARITY)


def reference_hooks(text: Any, *, limit: int = 2) -> list[str]:
    """从对方那句话里抽 1–2 个具体词当钩子（reference 提示词要用）。"""

    body = str(text or '')
    hooks: list[str] = []
    run: list[str] = []

    def flush() -> None:
        word = ''.join(run).strip()
        run.clear()
        while word and word[0] in _STOP_CHARS:
            word = word[1:]
        while word and word[-1] in _STOP_CHARS:
            word = word[:-1]
        if len(word) > 3:
            # 连着写的短句（"花盆打翻"）不是一个词：取前面两个字当具体词
            word = word[:2]
        if len(word) >= 2 and word not in _HOOK_STOP_WORDS and word not in hooks:
            hooks.append(word[:8])

    for ch in body:
        if ch.isspace() or ch in '，。！？、；：""\'\'（）()【】[]…—－-.,!?;:':
            flush()
            continue
        if ch in _STOP_CHARS:
            flush()
            continue
        run.append(ch)
        if len(run) >= 8:
            flush()
    flush()
    return hooks[: max(0, int(limit))]
