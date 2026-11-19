"""Product-owned immutable generation authority; never reconstructed from model text."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

from .experience import COMMUNICATIVE_ACTS


@dataclass(frozen=True)
class GenerationTopicContext:
    phase_id: str
    decision_id: str
    origin_session_id: str
    generated_at: float
    topics_json: str
    sources_json: str
    communicative_act: str = "reference"
    request_event_id: str = ""
    request_text: str = ""
    input_event_ids: tuple[str, ...] = ()

    @classmethod
    def capture(cls, *, phase_id: str, decision_id: str, origin_session_id: str,
                generated_at: float, topics: Iterable[dict[str, Any]],
                sources: dict[str, str], communicative_act: str = "reference",
                request_event_id: str = "", request_text: str = "",
                input_event_ids: Iterable[str] = ()) -> GenerationTopicContext:
        if any(not isinstance(value, str) or not value.strip()
               for value in (phase_id, decision_id, origin_session_id)):
            raise ValueError("generation context requires product identities")
        if isinstance(generated_at, bool) or not math.isfinite(float(generated_at)):
            raise ValueError("generation timestamp must be finite")
        if communicative_act not in COMMUNICATIVE_ACTS:
            raise ValueError("invalid generation communicative act")
        inputs = tuple(input_event_ids)
        if any(not isinstance(value, str) or not value.strip() for value in inputs):
            raise ValueError("invalid generation input identity")
        return cls(phase_id, decision_id, origin_session_id, float(generated_at),
                   json.dumps(list(topics), sort_keys=True, ensure_ascii=False, allow_nan=False),
                   json.dumps(dict(sources), sort_keys=True, ensure_ascii=False, allow_nan=False),
                   communicative_act, request_event_id, request_text, inputs)

    def topics(self) -> list[dict[str, Any]]:
        return json.loads(self.topics_json)

    def sources(self) -> dict[str, str]:
        return json.loads(self.sources_json)


@dataclass(frozen=True)
class GeneratedSpeechSegment:
    segment_id: str
    context: GenerationTopicContext
    target_session_id: str
    text: str
    delivery_decision_id: str = ""


def bind_generation_actions(actions: Iterable[Any], context: GenerationTopicContext) -> None:
    """Private in-memory association, outside raw/params/model serialization."""
    for action in actions:
        setattr(action, "_generation_topic_context", context)
