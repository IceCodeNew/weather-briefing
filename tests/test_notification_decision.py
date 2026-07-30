from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest

from weather_briefing.application.notification import weather_notification_assessment
from weather_briefing.composition.notifications import notification_decision_service
from weather_briefing.models import BriefingResult
from weather_briefing.notification_decision import (
    LLMPromptNotificationPolicy,
    NotificationAssessment,
    NotificationDecision,
    NotificationDecisionService,
)
from weather_briefing.notification_decision.policies import (
    SERVICE_STATUS_NOTIFICATION_KIND,
    SERVICE_STATUS_NOTIFICATION_PROMPT,
    WEATHER_NOTIFICATION_KIND,
    WEATHER_NOTIFICATION_PROMPT,
)


async def test_llm_prompt_policy_owns_prompt_and_copies_payload() -> None:
    model = AsyncMock()
    model.decide_notification.return_value = NotificationDecision(True)
    source_payload = {"current": {"status": "investigating"}}
    service = NotificationDecisionService(
        (
            LLMPromptNotificationPolicy(
                kind="service_status",
                system_prompt="Service-specific policy",
                model=model,
            ),
        )
    )

    decision = await service.assess_notification(NotificationAssessment(kind="service_status", payload=source_payload))

    assert decision.should_notify
    model.decide_notification.assert_awaited_once_with(
        "Service-specific policy",
        source_payload,
    )
    assert model.decide_notification.await_args.args[1] is not source_payload


async def test_message_type_can_use_non_llm_policy_logic() -> None:
    @dataclass(frozen=True, slots=True)
    class PriorityPolicy:
        kind: str = "priority"

        async def assess_notification(self, payload: Mapping[str, object]) -> NotificationDecision:
            return NotificationDecision(payload["priority"] == "critical")

    service = NotificationDecisionService((PriorityPolicy(),))

    decision = await service.assess_notification(
        NotificationAssessment(kind="priority", payload={"priority": "critical"})
    )

    assert decision.should_notify


@pytest.mark.parametrize("value", ("", " weather", "weather ", 1))
def test_assessment_rejects_unnormalized_kind(value: object) -> None:
    with pytest.raises(ValueError, match="non-empty normalized"):
        NotificationAssessment(kind=value, payload={})


@pytest.mark.parametrize("payload", (None, 1, [], {1: "value"}))
def test_assessment_rejects_invalid_payload(payload: object) -> None:
    with pytest.raises(ValueError, match="mapping with string keys"):
        NotificationAssessment(kind="weather", payload=payload)


def test_assessment_copies_valid_payload() -> None:
    payload = {"candidate_message": {"headline": "Rain soon"}}

    assessment = NotificationAssessment(kind="weather", payload=payload)

    assert assessment.payload == payload
    assert assessment.payload is not payload
    assert isinstance(assessment.payload, MappingProxyType)


@pytest.mark.parametrize(
    ("kind", "prompt", "message"),
    (
        ("", "prompt", "kind"),
        (" weather", "prompt", "kind"),
        (1, "prompt", "kind"),
        ("weather", " ", "prompt"),
        ("weather", 1, "prompt"),
    ),
)
def test_prompt_policy_rejects_invalid_registration(kind: object, prompt: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        LLMPromptNotificationPolicy(kind=kind, system_prompt=prompt, model=AsyncMock())


def test_decision_service_rejects_empty_and_duplicate_registries() -> None:
    with pytest.raises(ValueError, match="At least one"):
        NotificationDecisionService(())

    policy = LLMPromptNotificationPolicy(
        kind="weather",
        system_prompt="prompt",
        model=AsyncMock(),
    )
    with pytest.raises(ValueError, match="Duplicate"):
        NotificationDecisionService((policy, policy))


@pytest.mark.parametrize("kind", (None, 1, "", " weather"))
def test_decision_service_rejects_invalid_custom_policy_kind(kind: object) -> None:
    @dataclass(frozen=True, slots=True)
    class CustomPolicy:
        kind: object

        async def assess_notification(self, payload: Mapping[str, object]) -> NotificationDecision:
            return NotificationDecision(True)  # pragma: no cover - invalid registration cannot dispatch

    with pytest.raises(ValueError, match="Notification policy kind"):
        NotificationDecisionService((CustomPolicy(kind),))


async def test_decision_service_rejects_unknown_kind() -> None:
    service = NotificationDecisionService(
        (
            LLMPromptNotificationPolicy(
                kind="weather",
                system_prompt="prompt",
                model=AsyncMock(),
            ),
        )
    )

    with pytest.raises(ValueError, match="Unsupported notification kind: service_status"):
        await service.assess_notification(NotificationAssessment(kind="service_status", payload={}))


async def test_application_registry_composes_distinct_prompt_policies() -> None:
    model = AsyncMock()
    model.decide_notification.return_value = NotificationDecision(True)
    service = notification_decision_service(
        model,
        (WEATHER_NOTIFICATION_KIND, SERVICE_STATUS_NOTIFICATION_KIND),
    )

    await service.assess_notification(
        NotificationAssessment(kind=WEATHER_NOTIFICATION_KIND, payload={"candidate_message": {}})
    )
    await service.assess_notification(
        NotificationAssessment(kind=SERVICE_STATUS_NOTIFICATION_KIND, payload={"current": {}})
    )

    assert model.decide_notification.await_args_list[0].args[0] == WEATHER_NOTIFICATION_PROMPT
    assert model.decide_notification.await_args_list[1].args[0] == SERVICE_STATUS_NOTIFICATION_PROMPT
    assert WEATHER_NOTIFICATION_PROMPT != SERVICE_STATUS_NOTIFICATION_PROMPT


def test_application_registry_rejects_unknown_policy_kind() -> None:
    with pytest.raises(ValueError, match="Unsupported notification kind: future"):
        notification_decision_service(AsyncMock(), ("future",))


def test_weather_assessment_bounds_context_and_keeps_candidate_separate() -> None:
    payload = {
        "mode": "briefing",
        "now": "2026-07-29T09:00:00+08:00",
        "forecast_date": "2026-07-29",
        "location_scope": {"full_name": "Example"},
        "new_articles": [
            {
                "source_id": "article-new",
                "publisher": "Example",
                "title": "Rain approaching",
                "published_at": "2026-07-29T08:50:00+08:00",
                "content": "large article body",
                "url": "https://example.invalid/new",
                "verbatim": False,
            }
        ],
        "deferred_articles": [
            {
                "source_id": "article-deferred",
                "publisher": "Example",
                "title": "Earlier forecast",
                "published_at": "2026-07-29T07:00:00+08:00",
                "content": "another large article body",
                "url": "https://example.invalid/deferred",
                "verbatim": False,
            }
        ],
        "context_documents": [{"source_id": "weather", "content": "large current source body"}],
        "recent_context_documents": [{"source_id": "weather", "content": "large historical source body"}],
        "recent_briefings": [
            {"mode": "briefing", "published_at": "2026-07-29T07:00:00+08:00", "body": "Older briefing"},
            {"mode": "forecast", "published_at": "2026-07-29T08:45:00+08:00", "body": "Forecast"},
            {"mode": "briefing", "published_at": "2026-07-29T08:30:00+08:00", "body": "Latest briefing"},
        ],
        "currently_active_warnings": [
            {
                "id": "warning",
                "title": "Heat warning",
                "status": "active",
                "detail": "A long warning body that must not be duplicated.",
                "source_ids": ["warning-source"],
                "last_confirmed_at": "2026-07-29T08:45:00+08:00",
            }
        ],
    }
    result = BriefingResult(
        headline="Rain soon",
        headline_source_ids=("source",),
        conclusions=(),
        raw_payload={
            "headline": "Rain soon",
            "headline_source_ids": ["source"],
            "conclusions": [],
            "active_warnings": [],
            "resolved_warning_ids": [],
            "disaster_tracking": [],
            "advice": [],
        },
    )

    assessment = weather_notification_assessment(payload, result)

    assert assessment.kind == "weather"
    assert assessment.payload["candidate_message"] == result.raw_payload
    assert assessment.payload["mode"] == "briefing"
    assert assessment.payload["now"] == "2026-07-29T09:00:00+08:00"
    assert assessment.payload["forecast_date"] == "2026-07-29"
    assert assessment.payload["location_scope"] == {"full_name": "Example"}
    assert assessment.payload["previous_briefing"] == {
        "mode": "briefing",
        "published_at": "2026-07-29T08:30:00+08:00",
        "body": "Latest briefing",
    }
    new_articles = assessment.payload["new_articles"]
    assert isinstance(new_articles, list)
    assert new_articles == [
        {
            "source_id": "article-new",
            "publisher": "Example",
            "title": "Rain approaching",
            "published_at": "2026-07-29T08:50:00+08:00",
            "verbatim": False,
        }
    ]
    assert assessment.payload["deferred_articles"] == [
        {
            "source_id": "article-deferred",
            "publisher": "Example",
            "title": "Earlier forecast",
            "published_at": "2026-07-29T07:00:00+08:00",
            "verbatim": False,
        }
    ]
    assert assessment.payload["previous_active_warnings"] == [
        {
            "id": "warning",
            "title": "Heat warning",
            "status": "active",
            "last_confirmed_at": "2026-07-29T08:45:00+08:00",
        }
    ]
    assert isinstance(new_articles[0], dict)
    assert "content" not in new_articles[0]
    assert "context_documents" not in assessment.payload
    assert "recent_context_documents" not in assessment.payload
    assert "recent_briefings" not in assessment.payload


def test_weather_assessment_ignores_invalid_optional_collections() -> None:
    payload: dict[str, object] = {
        "mode": "briefing",
        "now": "2026-07-29T09:00:00+08:00",
        "forecast_date": "2026-07-29",
        "location_scope": {"full_name": "Example"},
        "new_articles": "invalid",
        "deferred_articles": [None, {1: "invalid key"}],
        "recent_briefings": None,
        "currently_active_warnings": [None, {1: "invalid key"}, {"id": "warning"}],
    }
    result = BriefingResult(
        headline="Routine weather",
        headline_source_ids=(),
        conclusions=(),
        raw_payload={},
    )

    assessment = weather_notification_assessment(payload, result)

    assert assessment.payload["new_articles"] == []
    assert assessment.payload["deferred_articles"] == []
    assert assessment.payload["previous_briefing"] is None
    assert assessment.payload["previous_active_warnings"] == [{"id": "warning"}]

    payload["currently_active_warnings"] = None
    assessment_without_warnings = weather_notification_assessment(payload, result)

    assert assessment_without_warnings.payload["previous_active_warnings"] == []


def test_weather_assessment_prefers_platform_neutral_previous_candidate() -> None:
    payload: dict[str, object] = {
        "mode": "briefing",
        "now": "2026-07-29T09:00:00+08:00",
        "forecast_date": "2026-07-29",
        "location_scope": {"full_name": "Example"},
        "new_articles": [],
        "deferred_articles": [],
        "recent_briefings": [
            {"mode": "briefing", "published_at": "2026-07-29T08:30:00+08:00", "body": "Platform body"}
        ],
        "currently_active_warnings": [],
    }
    result = BriefingResult(
        headline="Current headline",
        headline_source_ids=(),
        conclusions=(),
        raw_payload={"headline": "Current headline"},
    )
    previous_candidate = {"headline": "Previous headline", "conclusions": []}

    assessment = weather_notification_assessment(payload, result, previous_candidate)

    assert assessment.payload["previous_briefing"] == previous_candidate
