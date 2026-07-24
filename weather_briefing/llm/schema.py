"""Strict structured output schema shared by LLM adapters and domain validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from .base import LLMError, LLMRequestError


def _non_empty(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
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


class LLMStructuredOutput(_StrictLLMPayload):
    """Define the complete, strict response contract requested from every LLM."""

    headline: NonEmptyString
    headline_source_ids: CitedSourceIds
    conclusions: list[SourcedTextPayload]
    active_warnings: list[WarningPayload]
    resolved_warning_ids: list[NonEmptyString]
    disaster_tracking: list[SourcedTextPayload]
    advice: list[AdvicePayload]
    should_publish: bool


def validate_structured_output(payload: Mapping[str, Any]) -> LLMStructuredOutput:
    """Validate an application-provided structured response."""
    try:
        return LLMStructuredOutput.model_validate(payload)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.errors()[0]["loc"])
        raise LLMError(f"LLM response schema validation failed at {location}") from exc


def decode_structured_response(response: object) -> LLMStructuredOutput:
    """Decode the normalized any-llm response without masking programming errors."""
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise LLMRequestError("LLM returned no completion choices")
    message = getattr(choices[0], "message", None)
    if message is None:
        raise LLMRequestError("LLM completion choice is missing a message")
    parsed = getattr(message, "parsed", None)
    if isinstance(parsed, LLMStructuredOutput):
        return parsed
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise LLMRequestError("LLM returned empty JSON content")
    try:
        return LLMStructuredOutput.model_validate_json(content)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.errors()[0]["loc"])
        raise LLMError(f"LLM response schema validation failed at {location}") from exc
