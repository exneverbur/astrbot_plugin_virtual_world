"""Topics-only effective settings. No model imports, mutation or accumulated mapping."""
from dataclasses import dataclass
from collections.abc import Mapping
from types import MappingProxyType
import math

BASE = {'recall_limit': 3, 'proactive_limit': 1, 'proactive_min_importance': .5,
        'share_purpose_weight': 1., 'experience_willingness': .5, 'expression_length': 120}
SOFT_MAPPING = {'share_purpose_weight': ('chat_freq', (.25, .6, 1., 1.4, 1.8)),
                'experience_willingness': ('self_disclosure', (0., .25, .5, .75, 1.)),
                'expression_length': ('verbosity', (40, 80, 120, 180, 240))}
LEVELS = ((0, 0, .8), (1, 1, .65), (3, 1, .5), (5, 2, .35), (8, 3, .2))

def _dict(value):
    if isinstance(value, Mapping):
        return dict(value)
    method = getattr(value, 'model_dump', None)
    return method() if method else {}

@dataclass(frozen=True)
class EffectiveTopicSettings:
    enabled: bool
    recall_limit: int
    proactive_limit: int
    proactive_min_importance: float
    sources: Mapping[str, str]
    share_purpose_weight: float = 1.
    experience_willingness: float = .5
    expression_length: int = 120

    @property
    def values(self):
        return {'enabled': self.enabled, 'recall_limit': self.recall_limit,
                'proactive_limit': self.proactive_limit,
                'proactive_min_importance': self.proactive_min_importance,
                'share_purpose_weight': self.share_purpose_weight,
                'experience_willingness': self.experience_willingness,
                'expression_length': self.expression_length}

    def to_dict(self):
        return {'values': self.values, 'sources': dict(self.sources)}


def topic_effective_config(world) -> EffectiveTopicSettings:
    data = _dict(world)
    topics = _dict(data.get('topics', {}))
    knobs = _dict(data.get('ui_knobs', {}))
    overrides = _dict(topics.get('overrides', {}))
    values, sources = {}, {}
    level = knobs.get('self_disclosure')
    level = level if type(level) is int and 1 <= level <= 5 else None
    for index, (name, default) in enumerate(BASE.items()):
        value = topics.get(name, default)
        source = 'base'
        override = _dict(overrides.get(name, {}))
        # Raw legacy mapping also retains explicit old fields as custom.
        mode = override.get('mode', 'custom' if name in topics and not overrides and topics.get('settings_version') != 1 else 'auto')
        if name in SOFT_MAPPING:
            knob_name, mapped = SOFT_MAPPING[name]
            knob_level = knobs.get(knob_name)
            if type(knob_level) is int and 1 <= knob_level <= 5:
                value, source = mapped[knob_level - 1], 'handfeel'
        elif level is not None:
            value, source = LEVELS[level - 1][index], 'handfeel'
        if mode == 'custom':
            value, source = override.get('value', topics.get(name, default)), 'custom'
        try:
            value = float(value)
            if not math.isfinite(value):
                raise ValueError()
        except (ValueError, TypeError):
            value, source = default, 'invalid_fallback'
        if name in ('proactive_min_importance', 'experience_willingness'):
            value = min(1., max(0., value))
        elif name == 'share_purpose_weight':
            value = max(0., value)
        else:
            value = max(0, int(value))
        values[name], sources[name] = value, source
    enabled = topics.get('enabled', True) is True
    if not enabled:
        for name in ('recall_limit', 'proactive_limit', *SOFT_MAPPING):
            values[name] = 0
            sources[name] = 'hard_gate'
    return EffectiveTopicSettings(enabled=enabled, sources=MappingProxyType(sources), **values)

resolve_world_topic_settings = topic_effective_config
