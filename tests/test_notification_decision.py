from collections.abc import Mapping
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from weather_briefing.notification_decision import (
    LLMPromptNotificationPolicy,
    NotificationAssessment,
    NotificationDecision,
    NotificationDecisionService,
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
