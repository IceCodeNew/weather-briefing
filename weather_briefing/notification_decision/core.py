"""Portable notification-decision contracts and policy dispatch."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, TypeGuard


@dataclass(frozen=True, slots=True, init=False)
class NotificationDecision:
    """State whether one candidate is worth interrupting the user."""

    should_notify: bool

    def __init__(self, should_notify: object) -> None:
        """Reject non-boolean decisions at the portable policy boundary."""
        if not isinstance(should_notify, bool):
            msg = "Notification decision should_notify must be a boolean"
            raise ValueError(msg)  # noqa: TRY004
        object.__setattr__(self, "should_notify", should_notify)


def _validated_kind(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        msg = f"{context} must be a non-empty normalized string"
        raise ValueError(msg)
    return value


def _is_string_object_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping) and all(isinstance(key, str) for key in value)


def _validated_payload(value: object) -> Mapping[str, object]:
    if not _is_string_object_mapping(value):
        msg = "Notification payload must be a mapping with string keys"
        raise ValueError(msg)
    return MappingProxyType(dict(value))


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
    def kind(self) -> object:
        """Return the untrusted message type identifier declared by this policy."""
        ...

    async def assess_notification(
        self,
        payload: Mapping[str, object],
    ) -> object:
        """Evaluate one candidate payload and return an untrusted decision."""
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
            msg = "Notification policy prompt must not be empty"
            raise ValueError(msg)
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
            kind = _validated_kind(policy.kind, context="Notification policy kind")
            if kind in registered:
                msg = f"Duplicate notification policy: {kind}"
                raise ValueError(msg)
            registered[kind] = policy
        if not registered:
            msg = "At least one notification policy is required"
            raise ValueError(msg)
        self._policies = registered

    async def assess_notification(
        self,
        assessment: NotificationAssessment,
    ) -> NotificationDecision:
        """Evaluate a candidate with the policy registered for its message type."""
        policy = self._policies.get(assessment.kind)
        if policy is None:
            msg = f"Unsupported notification kind: {assessment.kind}"
            raise ValueError(msg)
        decision = await policy.assess_notification(assessment.payload)
        if not isinstance(decision, NotificationDecision):
            msg = "Notification policy must return a NotificationDecision"
            raise ValueError(msg)  # noqa: TRY004
        return decision
