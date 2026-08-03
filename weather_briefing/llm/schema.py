"""Strict structured output schema shared by LLM adapters and domain validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeAlias

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from .base import LLMError, LLMRequestError

if TYPE_CHECKING:
    from collections.abc import Mapping


def _non_empty(value: str) -> str:
    if not value.strip():
        msg = "must not be empty"
        raise ValueError(msg)
    return value


NonEmptyString: TypeAlias = Annotated[str, AfterValidator(_non_empty)]
CitedSourceIds: TypeAlias = Annotated[list[NonEmptyString], Field(min_length=1)]


class _StrictLLMPayload(BaseModel):
    """Reject coercion, defaults, and undeclared fields at the LLM boundary."""

    model_config = ConfigDict(extra="forbid", strict=True)


class SourcedTextPayload(_StrictLLMPayload):
    """Describe one source-cited statement in the model response."""

    text: NonEmptyString
    source_ids: CitedSourceIds


class WarningPayload(_StrictLLMPayload):
    """Describe one active warning in the model response."""

    id: NonEmptyString
    title: NonEmptyString
    status: NonEmptyString
    detail: NonEmptyString
    source_ids: CitedSourceIds


class AdvicePayload(SourcedTextPayload):
    """Describe one categorized lifestyle recommendation."""

    topic: Literal["clothing", "dehumidification", "exercise", "mask", "allergen"]


class ServiceStatusTranslationOutput(_StrictLLMPayload):
    """Return one faithful rendering of an official incident update."""

    title: NonEmptyString
    body: NonEmptyString


class NotificationDecisionOutput(_StrictLLMPayload):
    """Return an information-type-neutral notification decision."""

    should_notify: bool


class LLMStructuredOutput(_StrictLLMPayload):
    """Define the complete, strict response contract requested from every LLM."""

    headline: NonEmptyString
    headline_source_ids: CitedSourceIds
    conclusions: list[SourcedTextPayload]
    active_warnings: list[WarningPayload]
    resolved_warning_ids: list[NonEmptyString]
    disaster_tracking: list[SourcedTextPayload]
    advice: list[AdvicePayload]


def validate_structured_output(payload: Mapping[str, Any]) -> LLMStructuredOutput:
    """Validate an application-provided structured response."""
    try:
        return LLMStructuredOutput.model_validate(payload)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.errors()[0]["loc"])
        msg = f"LLM response schema validation failed at {location}"
        raise LLMError(msg) from exc


def decode_structured_response(response: object) -> LLMStructuredOutput:
    """Decode the normalized any-llm response without masking programming errors."""
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        msg = "LLM returned no completion choices"
        raise LLMRequestError(msg)
    message = getattr(choices[0], "message", None)
    if message is None:
        msg = "LLM completion choice is missing a message"
        raise LLMRequestError(msg)
    parsed = getattr(message, "parsed", None)
    if isinstance(parsed, LLMStructuredOutput):
        return parsed
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        msg = "LLM returned empty JSON content"
        raise LLMRequestError(msg)
    try:
        return LLMStructuredOutput.model_validate_json(content)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.errors()[0]["loc"])
        msg = f"LLM response schema validation failed at {location}"
        raise LLMError(msg) from exc


def decode_service_status_translation(response: object) -> ServiceStatusTranslationOutput:
    """Decode one strict service-status translation response."""
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        msg = "LLM returned no completion choices"
        raise LLMRequestError(msg)
    message = getattr(choices[0], "message", None)
    if message is None:
        msg = "LLM completion choice is missing a message"
        raise LLMRequestError(msg)
    parsed = getattr(message, "parsed", None)
    if isinstance(parsed, ServiceStatusTranslationOutput):
        return parsed
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        msg = "LLM returned empty JSON content"
        raise LLMRequestError(msg)
    try:
        return ServiceStatusTranslationOutput.model_validate_json(content)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.errors()[0]["loc"])
        msg = f"LLM response schema validation failed at {location}"
        raise LLMError(msg) from exc


def decode_notification_decision(response: object) -> bool:
    """Decode one strict notification-decision response."""
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        msg = "LLM returned no completion choices"
        raise LLMRequestError(msg)
    message = getattr(choices[0], "message", None)
    if message is None:
        msg = "LLM completion choice is missing a message"
        raise LLMRequestError(msg)
    parsed = getattr(message, "parsed", None)
    if isinstance(parsed, NotificationDecisionOutput):
        return parsed.should_notify
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        msg = "LLM returned empty JSON content"
        raise LLMRequestError(msg)
    try:
        return NotificationDecisionOutput.model_validate_json(content).should_notify
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.errors()[0]["loc"])
        msg = f"LLM response schema validation failed at {location}"
        raise LLMError(msg) from exc
