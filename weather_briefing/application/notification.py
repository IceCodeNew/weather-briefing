"""Weather-specific notification assessment input."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, TypeGuard

from weather_briefing.notification_decision import NotificationAssessment
from weather_briefing.notification_decision.policies import WEATHER_NOTIFICATION_KIND

if TYPE_CHECKING:
    from weather_briefing.models import BriefingResult

_WEATHER_DECISION_SCALAR_KEYS = (
    "mode",
    "now",
    "forecast_date",
    "location_scope",
)
_ARTICLE_METADATA_KEYS = ("source_id", "publisher", "title", "published_at", "verbatim")
_WARNING_METADATA_KEYS = ("id", "title", "status", "last_confirmed_at")


def _is_string_object_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping) and all(isinstance(key, str) for key in value)


def _compact_articles(value: object) -> list[dict[str, object]]:
    """Keep freshness and identity metadata without resending article bodies."""
    if not isinstance(value, list):
        return []
    compacted: list[dict[str, object]] = []
    for item in value:
        if not _is_string_object_mapping(item):
            continue
        compacted.append({key: item[key] for key in _ARTICLE_METADATA_KEYS if key in item})
    return compacted


def _compact_warnings(value: object) -> list[dict[str, object]]:
    """Keep warning identity and lifecycle metadata without duplicating details."""
    if not isinstance(value, list):
        return []
    compacted: list[dict[str, object]] = []
    for item in value:
        if not _is_string_object_mapping(item):
            continue
        compacted.append({key: item[key] for key in _WARNING_METADATA_KEYS if key in item})
    return compacted


def _latest_briefing(value: object) -> object | None:
    """Return the latest successful briefing baseline, excluding forecasts."""
    if not isinstance(value, list):
        return None
    return next(
        (item for item in reversed(value) if _is_string_object_mapping(item) and item.get("mode") == "briefing"),
        None,
    )


def weather_notification_assessment(
    briefing_payload: Mapping[str, object],
    result: BriefingResult,
    previous_candidate_message: Mapping[str, object] | None = None,
) -> NotificationAssessment:
    """Build a bounded weather-policy input without duplicating source bodies."""
    policy_input = {key: briefing_payload[key] for key in _WEATHER_DECISION_SCALAR_KEYS}
    policy_input["new_articles"] = _compact_articles(briefing_payload.get("new_articles"))
    policy_input["deferred_articles"] = _compact_articles(briefing_payload.get("deferred_articles"))
    policy_input["previous_briefing"] = (
        previous_candidate_message
        if previous_candidate_message is not None
        else _latest_briefing(briefing_payload.get("recent_briefings"))
    )
    policy_input["previous_active_warnings"] = _compact_warnings(briefing_payload.get("currently_active_warnings"))
    policy_input["candidate_message"] = result.raw_payload
    return NotificationAssessment(
        kind=WEATHER_NOTIFICATION_KIND,
        payload=policy_input,
    )
