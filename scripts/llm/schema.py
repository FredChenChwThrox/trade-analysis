"""LLM 输出 JSON Schema 与校验（r2 §6.2）。

校验失败（枚举非法/长度超限/缺必填）→ 该条丢弃不写库（不冒充）；
禁用词命中不在这里拒——由 eval 层 gate 判 needs_review（r2 §6.3）。
"""

from __future__ import annotations

import jsonschema

EVENT_ASSESSMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scope", "direction", "materiality", "confidence",
                 "action_hint", "rationale"],
    "properties": {
        "scope": {"enum": ["macro", "policy", "industry", "company", "flow"]},
        "direction": {"enum": ["positive", "negative", "neutral"]},
        "materiality": {"enum": ["low", "medium", "high", "critical"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "target": {"enum": ["eps", "pe", "sentiment", None]},
        "half_life": {"enum": ["day", "week", "month", "quarter", None]},
        "expectation_gap": {"type": ["string", "null"], "maxLength": 200},
        "action_hint": {"enum": ["none", "swing", "schedule", "redraw_anchor"]},
        "falsification_suggestion": {"type": ["string", "null"], "maxLength": 200},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 300},
    },
}

NARRATIVE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["narrative"],
    "properties": {
        "narrative": {"type": ["string", "null"], "maxLength": 150},
    },
}


def validate_event(obj: dict) -> None:
    """合法直接返回；非法抛 jsonschema.ValidationError（调用方按丢弃处理）。"""
    jsonschema.validate(obj, EVENT_ASSESSMENT_SCHEMA)


def validate_narrative(obj: dict) -> None:
    jsonschema.validate(obj, NARRATIVE_SCHEMA)
