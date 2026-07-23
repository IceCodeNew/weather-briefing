"""Conversion from validated LLM output to briefing domain models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pendulum

from ..models import Advice, AdviceTopic, BriefingResult, Conclusion, Warning
from ..time_utils import require_aware_datetime
from .base import LLMError
from .schema import SourcedTextPayload, validate_structured_output


def parse_result(
    payload: Mapping[str, Any],
    now: pendulum.DateTime,
    valid_source_ids: set[str],
) -> BriefingResult:
    """Validate an LLM payload and convert it to a briefing result."""
    require_aware_datetime(now, context="Briefing result time")
    structured = validate_structured_output(payload)

    def cited_source_ids(source_ids: list[str]) -> tuple[str, ...]:
        unknown = set(source_ids) - valid_source_ids
        if unknown:
            raise LLMError(f"Model cited unknown source IDs: {sorted(unknown)}")
        return tuple(source_ids)

    def sourced_text_items(values: list[SourcedTextPayload]) -> tuple[Conclusion, ...]:
        return tuple(Conclusion(text=value.text, source_ids=cited_source_ids(value.source_ids)) for value in values)

    warnings = tuple(
        Warning(
            id=value.id,
            title=value.title,
            status=value.status,
            detail=value.detail,
            source_ids=cited_source_ids(value.source_ids),
            last_confirmed_at=now,
        )
        for value in structured.active_warnings
    )
    advice = tuple(
        Advice(
            topic=AdviceTopic(value.topic),
            text=value.text,
            source_ids=cited_source_ids(value.source_ids),
        )
        for value in structured.advice
    )
    raw_payload = structured.model_dump(mode="json")
    return BriefingResult(
        headline=structured.headline,
        headline_source_ids=cited_source_ids(structured.headline_source_ids),
        conclusions=sourced_text_items(structured.conclusions),
        active_warnings=warnings,
        resolved_warning_ids=tuple(structured.resolved_warning_ids),
        advice=advice,
        disaster_tracking=sourced_text_items(structured.disaster_tracking),
        should_publish=structured.should_publish,
        raw_payload=raw_payload,
    )
