"""Portable notification-decision contracts and policy dispatch."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, TypeGuard


@dataclass(frozen=True, slots=True)
class NotificationDecision:
    """State whether one candidate is worth interrupting the user."""

    should_notify: bool


def _validated_kind(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{context} must be a non-empty normalized string")
    return value


def _is_string_object_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping) and all(isinstance(key, str) for key in value)


def _validated_payload(value: object) -> dict[str, object]:
    if not _is_string_object_mapping(value):
        raise ValueError("Notification payload must be a mapping with string keys")
    return dict(value)


@dataclass(frozen=True, slots=True, init=False)
class NotificationAssessment:
    """Pair a message type with the facts needed by its notification policy."""

    kind: str
    payload: Mapping[str, object]

    def __init__(self, kind: object, payload: object) -> None:
        """Validate and retain one application-owned policy identifier and payload."""
        object.__setattr__(self, "kind", _validated_kind(kind, context="Notification kind"))
        object.__setattr__(self, "payload", _validated_payload(payload))


class NotificationDecisionModel(Protocol):
    """Evaluate one prompt and payload through a structured model adapter."""

    async def decide_notification(
        self,
        system_prompt: str,
        payload: dict[str, object],
    ) -> NotificationDecision:
        """Return one strict notification decision."""
        ...


class NotificationPolicy(Protocol):
    """Own the decision behavior for one message type."""

    @property
    def kind(self) -> str:
        """Return the message type handled by this policy."""
        ...

    async def assess_notification(
        self,
        payload: Mapping[str, object],
    ) -> NotificationDecision:
        """Evaluate one candidate payload."""
        ...


@dataclass(frozen=True, slots=True, init=False)
class LLMPromptNotificationPolicy:
    """Evaluate one message type with its own prompt and model boundary."""

    kind: str
    system_prompt: str
    model: NotificationDecisionModel

    def __init__(
        self,
        kind: object,
        system_prompt: object,
        model: NotificationDecisionModel,
    ) -> None:
        """Validate and retain one prompt-driven policy registration."""
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("Notification policy prompt must not be empty")
        object.__setattr__(self, "kind", _validated_kind(kind, context="Notification policy kind"))
        object.__setattr__(self, "system_prompt", system_prompt)
        object.__setattr__(self, "model", model)

    async def assess_notification(
        self,
        payload: Mapping[str, object],
    ) -> NotificationDecision:
        """Evaluate a candidate without exposing policy selection to the model adapter."""
        return await self.model.decide_notification(self.system_prompt, dict(payload))


class NotificationDecisionProvider(Protocol):
    """Dispatch notification assessments without exposing registered policies."""

    async def assess_notification(
        self,
        assessment: NotificationAssessment,
    ) -> NotificationDecision:
        """Evaluate one typed notification candidate."""
        ...


class NotificationDecisionService:
    """Dispatch each message type to one explicit notification policy."""

    def __init__(self, policies: Iterable[NotificationPolicy]) -> None:
        """Build an immutable policy registry and reject duplicate kinds."""
        registered: dict[str, NotificationPolicy] = {}
        for policy in policies:
            if policy.kind in registered:
                raise ValueError(f"Duplicate notification policy: {policy.kind}")
            registered[policy.kind] = policy
        if not registered:
            raise ValueError("At least one notification policy is required")
        self._policies = registered

    async def assess_notification(
        self,
        assessment: NotificationAssessment,
    ) -> NotificationDecision:
        """Evaluate a candidate with the policy registered for its message type."""
        policy = self._policies.get(assessment.kind)
        if policy is None:
            raise ValueError(f"Unsupported notification kind: {assessment.kind}")
        return await policy.assess_notification(assessment.payload)
