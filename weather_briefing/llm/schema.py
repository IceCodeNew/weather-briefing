"""Strict structured output schema shared by LLM adapters and domain validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from .base import LLMError, LLMRequestError

_MARKDOWN_PATTERNS = (
    re.compile(r"(?m)^\s{0,3}(?:#{1,6}|[-*+>]|\d+[.)])\s+"),
    re.compile(r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$"),
    re.compile(r"```|~~~"),
    re.compile(r"\[[^\]\n]+\]\([^)\n]+\)"),
    re.compile(r"(?P<delimiter>\*\*|__)(?=\S).+?(?<=\S)(?P=delimiter)"),
    re.compile(r"(?<!\*)\*(?!\*)(?=\S)[^*\n]+?(?<=\S)\*(?!\*)"),
    re.compile(r"(?<![\w_])_(?!_)(?=\S)[^_\n]+?(?<=\S)_(?![\w_])"),
    re.compile(r"~~(?=\S).+?(?<=\S)~~"),
    re.compile(r"`[^`\n]+`"),
)


def _non_empty(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value


def _plain_text(value: str) -> str:
    if any(pattern.search(value) for pattern in _MARKDOWN_PATTERNS):
        raise ValueError("must not contain Markdown syntax")
    return value


NonEmptyString: TypeAlias = Annotated[str, AfterValidator(_non_empty)]
PlainTextString: TypeAlias = Annotated[str, AfterValidator(_non_empty), AfterValidator(_plain_text)]
CitedSourceIds: TypeAlias = Annotated[list[NonEmptyString], Field(min_length=1)]


class _StrictLLMPayload(BaseModel):
    """Reject coercion, defaults, and undeclared fields at the LLM boundary."""

    model_config = ConfigDict(extra="forbid", strict=True)


class SourcedTextPayload(_StrictLLMPayload):
    """Describe one source-cited statement in the model response."""

    text: PlainTextString
    source_ids: CitedSourceIds


class WarningPayload(_StrictLLMPayload):
    """Describe one active warning in the model response."""

    id: NonEmptyString
    title: PlainTextString
    status: PlainTextString
    detail: PlainTextString
    source_ids: CitedSourceIds


class AdvicePayload(SourcedTextPayload):
    """Describe one categorized lifestyle recommendation."""

    topic: Literal["clothing", "dehumidification", "exercise", "mask", "allergen"]


class LLMStructuredOutput(_StrictLLMPayload):
    """Define the complete, strict response contract requested from every LLM."""

    headline: PlainTextString
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
