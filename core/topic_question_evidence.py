"""Bind a real user question to exact topic sources; model output grants nothing."""
from __future__ import annotations
import re
from collections.abc import Mapping
from .topic_policy import RequestEvidence


def question_evidence(topic, sources: Mapping, *, session_id: str, event_id: str,
                      text: str, now: float, latest_dream_time: float = 0.0) -> RequestEvidence:
    ids = tuple(topic.source_event_ids)
    denied = RequestEvidence(session_id=session_id, event_id=event_id)
    if not event_id or not ids or not text.strip():
        return denied
    rows = [sources.get(key) for key in ids]
    if any(not isinstance(row, Mapping) or row.get('origin_session_id') != session_id for row in rows):
        return denied
    query = text.strip()
    # Imperatives to recount and actual questions count. A statement about dreaming does not.
    asking = bool(re.search(r'[?？]|什么|怎么|哪|有没有|是否|能不能|讲讲|说说|告诉我|还记得', query))
    if not asking:
        return denied
    quoted = re.findall(r'[「“\"]([^」”\"]{4,})[」”\"]', query)
    details = topic.details or {}
    body = '\n'.join([topic.summary, *[str(fact) for fact in details.get('facts', [])]])
    exact = topic.topic_id in query or any(fragment in body for fragment in quoted)
    if exact:
        return RequestEvidence(session_id, event_id, True, ids)
    if topic.source_kind != 'dream' or not re.search(r'你.{0,8}(?:梦|做梦)|(?:讲讲|说说|告诉我).{0,8}梦', query):
        return denied
    if any((row.get('reality_kind') != 'fiction' or row.get('event_kind') != 'dream') for row in rows):
        return denied
    # Generic dream questions select only the most recent captured dream, never the whole archive.
    times = [float(row.get('occurred_at') or 0) for row in rows]
    if not times or min(times) <= 0 or max(times) != latest_dream_time or max(times) > now:
        return denied
    age = now - max(times)
    if re.search(r'昨晚|昨天|昨夜', query):
        valid_time = 0 <= age <= 48 * 3600
    elif re.search(r'今天|今早|刚才|刚刚', query):
        valid_time = 0 <= age <= 24 * 3600
    else:
        valid_time = 0 <= age <= 7 * 24 * 3600
    return RequestEvidence(session_id, event_id, valid_time, ids if valid_time else ())
