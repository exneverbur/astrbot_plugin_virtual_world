"""Validate optional model proposals against this turn's authoritative sources.

This module does not establish the truth of arbitrary prose. Facts come from
captured source payloads; summary is presentation only, never evidence.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .topic_policy import normalize_policy, merge_policies, tighten_policy, _get, _ids, _text, _data
from .experience import Topic


@dataclass(frozen=True)
class TopicProposal:
    summary: str
    source_event_ids: tuple[str, ...]
    visibility: Mapping[str, Any]
    facts: tuple[str, ...] = ()
    share_angle: str = ''
    relevant_contexts: tuple[str, ...] = ()
    timeliness: str = ''
    reality_kind: str = 'real'


@dataclass(frozen=True)
class ProposalResult:
    proposal: TopicProposal | None
    reason: str

    def __bool__(self) -> bool:
        return self.proposal is not None


def validate_topic_proposal(raw: Any, *, allowed_source_event_ids: Iterable[str],
                            sources: Mapping[str, Any], scope_id: str,
                            now: float | None = None) -> ProposalResult:
    """At most one optional proposal. Unknown IDs, unknown origin or no facts deny.

    allowed_source_event_ids is constructed by the host from this turn's actual
    input/results. sources must be authoritative rows, not model-generated rows.
    Legacy sources remain private and cannot silently become trusted_rule.
    """
    if raw is None:
        return ProposalResult(None, 'no proposal')
    if not isinstance(raw, Mapping):
        return ProposalResult(None, 'proposal must be one object')
    summary = _text(raw.get('summary'))
    raw_ids = raw.get('source_event_ids')
    if not summary or len(summary) > 4000 or not isinstance(raw_ids, (list, tuple)) or not raw_ids or any(not isinstance(x, str) or not x.strip() for x in raw_ids):
        return ProposalResult(None, 'invalid summary or source IDs')
    ids = _ids(raw_ids)
    if not set(ids) <= set(allowed_source_event_ids):
        return ProposalResult(None, 'source outside turn whitelist')
    if not isinstance(sources, Mapping):
        return ProposalResult(None, 'source lookup unavailable')
    policies, facts, realities = [], [], set()
    for source_id in ids:
        source = sources.get(source_id)
        if source is None or _get(source, 'event_id', '') != source_id:
            return ProposalResult(None, 'missing or mismatched source')
        from .topic_policy import source_is_withdrawn
        if source_is_withdrawn(source):
            return ProposalResult(None, 'source withdrawn or unknown status')
        expiry = _get(source, 'expires_at', None)
        if expiry is not None:
            try:
                import math
                expired = now is None or not math.isfinite(float(expiry)) or float(expiry) <= float(now)
            except (ValueError, TypeError):
                expired = True
            if expired:
                return ProposalResult(None, 'source expiry unknown or expired')
        source_scope = _get(source, 'scope_id', '')
        origin = _get(source, 'origin_session_id', '')
        if not scope_id or source_scope != scope_id or not origin:
            return ProposalResult(None, 'source scope or exact origin unknown')
        policy = normalize_policy(_get(source, 'visibility', _get(source, 'visibility_json', {})),
                                  scope_id=source_scope, origin_session_id=origin)
        if policy.get('deny_all') or not policy['origin_session_id']:
            return ProposalResult(None, 'damaged or conflicting source policy')
        policies.append(policy)
        payload = _data(_get(source, 'payload', _get(source, 'payload_json', {})))
        if not isinstance(payload, Mapping):
            payload = {}
        # Only captured facts/text/observations count. Model facts are ignored.
        captured = payload.get('facts', ())
        if isinstance(captured, (list, tuple)):
            facts.extend(_text(x) for x in captured if _text(x))
        for name in ('text', 'result', 'observation'):
            fact = _text(payload.get(name))
            if fact:
                facts.append(fact)
        realities.add(_get(source, 'reality_kind', 'legacy_unknown'))
    if not facts:
        return ProposalResult(None, 'no captured information or actual result')
    # An absent/invalid sensitivity is conservative private. All model fields
    # that could otherwise confer authority/session/origin are discarded.
    suggestion = {'sensitivity': raw.get('sensitivity'),
                  'subject_ids': raw.get('subject_ids', []),
                  'hard_private': raw.get('hard_private') is True}
    policies = [tighten_policy(p, suggestion) for p in policies]
    reality = 'unreal' if realities & {'fiction', 'generated', 'generated_media', 'unreal'} else ('real' if realities <= {'real', 'tool'} else 'legacy_unknown')
    if reality != 'real':
        policies = [tighten_policy(p, {'sensitivity': 'private'}) for p in policies]
    proposal = TopicProposal(summary, tuple(ids), merge_policies(*policies),
                             tuple(dict.fromkeys(facts)), _text(raw.get('share_angle')),
                             tuple(_ids(raw.get('relevant_contexts', []))),
                             _text(raw.get('timeliness')), reality)
    return ProposalResult(proposal, 'validated against captured sources')


def apply_topic_proposal(existing: Topic | None, proposal: TopicProposal, *, topic_id: str,
                         scope_id: str, now: float, source_kind: str = 'chat',
                         expires_at: float | None = None) -> Topic:
    """Pure materialization; same source + captured facts = replay/no new revision.

    Different source identities are a different experience, even with identical
    wording. Caller chooses the stable event-derived topic_id, never a theme hash.
    """
    if not isinstance(proposal, TopicProposal) or not topic_id or not scope_id:
        raise ValueError('validated proposal and explicit identity required')
    if existing is not None and (existing.topic_id != topic_id or existing.scope_id != scope_id or
                                 set(existing.source_event_ids) != set(proposal.source_event_ids)):
        raise ValueError('different source experience must use a different topic identity')
    details = dict(existing.details) if existing else {}
    visibility = proposal.visibility
    if existing is not None and 'visibility' in details and details['visibility'] != visibility:
        # Each complete version is an AND, never union sessions or unlock.
        visibility = merge_policies(details['visibility'], visibility)
    details.update({'facts': list(proposal.facts), 'visibility': visibility,
                    'share_angle': proposal.share_angle,
                    'relevant_contexts': list(proposal.relevant_contexts),
                    'timeliness': proposal.timeliness, 'reality_kind': proposal.reality_kind})
    if existing is None:
        return Topic(topic_id=topic_id, scope_id=scope_id,
                     source_event_ids=proposal.source_event_ids, summary=proposal.summary,
                     details=details, source_kind=source_kind,
                     created_at=now, updated_at=now, expires_at=expires_at)
    # Normalize facts as a set: reordering does not constitute a new fact.
    fact_changed = set(_ids(existing.details.get('facts', []))) != set(proposal.facts)
    if existing.summary == proposal.summary and existing.details == details and not fact_changed:
        return existing
    return replace(existing, summary=proposal.summary, details=details,
                   information_revision=existing.information_revision + int(fact_changed),
                   version=existing.version + 1, updated_at=now)
