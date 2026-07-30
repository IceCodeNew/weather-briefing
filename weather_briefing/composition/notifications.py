"""Runtime composition of notification policies."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..notification_decision import (
    LLMPromptNotificationPolicy,
    NotificationDecisionModel,
    NotificationDecisionService,
    NotificationPolicy,
)
from ..notification_decision.policies import (
    SERVICE_STATUS_NOTIFICATION_KIND,
    SERVICE_STATUS_NOTIFICATION_PROMPT,
    WEATHER_NOTIFICATION_KIND,
    WEATHER_NOTIFICATION_PROMPT,
)

NotificationPolicyBuilder = Callable[[NotificationDecisionModel], NotificationPolicy]


def _prompt_policy(
    kind: str,
    prompt: str,
) -> NotificationPolicyBuilder:
    """Build a factory for one prompt-driven policy."""

    def build(model: NotificationDecisionModel) -> NotificationPolicy:
        return LLMPromptNotificationPolicy(
            kind=kind,
            system_prompt=prompt,
            model=model,
        )

    return build


NOTIFICATION_POLICY_BUILDERS: dict[str, NotificationPolicyBuilder] = {
    WEATHER_NOTIFICATION_KIND: _prompt_policy(
        WEATHER_NOTIFICATION_KIND,
        WEATHER_NOTIFICATION_PROMPT,
    ),
    SERVICE_STATUS_NOTIFICATION_KIND: _prompt_policy(
        SERVICE_STATUS_NOTIFICATION_KIND,
        SERVICE_STATUS_NOTIFICATION_PROMPT,
    ),
}


def notification_decision_service(
    model: NotificationDecisionModel,
    kinds: Sequence[str],
) -> NotificationDecisionService:
    """Compose only the policies needed by one application workflow."""
    policies: list[NotificationPolicy] = []
    for kind in kinds:
        builder = NOTIFICATION_POLICY_BUILDERS.get(kind)
        if builder is None:
            raise ValueError(f"Unsupported notification kind: {kind}")
        policies.append(builder(model))
    return NotificationDecisionService(policies)
