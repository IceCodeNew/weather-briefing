"""LLM provider adapters and structured result validation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Protocol, TypeAlias

import httpx
import pendulum
from any_llm import AnyLLM
from any_llm.exceptions import AnyLLMError
from any_llm.providers.openai.base import BaseOpenAIProvider
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from .api_client import api_call_context
from .models import Advice, AdviceTopic, BriefingResult, Conclusion, Warning
from .time_utils import require_aware_datetime


class LLMError(RuntimeError):
    """Raised when the model response is unavailable or violates its contract."""


class LLMProvider(Protocol):
    """Produce a structured briefing payload from validated source context."""

    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        """Return one structured model response."""
        ...


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


def _validate_structured_output(payload: Mapping[str, Any]) -> LLMStructuredOutput:
    try:
        return LLMStructuredOutput.model_validate(payload)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.errors()[0]["loc"])
        raise LLMError(f"LLM response schema validation failed at {location}") from exc


def _decode_structured_response(response: Any) -> LLMStructuredOutput:
    """Decode the normalized any-llm response without masking programming errors."""
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise LLMError("LLM returned no completion choices")
    message = getattr(choices[0], "message", None)
    if message is None:
        raise LLMError("LLM completion choice is missing a message")
    parsed = getattr(message, "parsed", None)
    if isinstance(parsed, LLMStructuredOutput):
        return parsed
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content:
        raise LLMError("LLM returned empty JSON content")
    try:
        return LLMStructuredOutput.model_validate_json(content)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.errors()[0]["loc"])
        raise LLMError(f"LLM response schema validation failed at {location}") from exc


class AnyLLMStructuredProvider:
    """Adapt an any-llm provider to the application's structured LLM boundary."""

    def __init__(
        self,
        client: AnyLLM,
        *,
        provider: str,
        model: str,
        max_output_tokens: int,
    ) -> None:
        """Configure a reusable any-llm client and output limit."""
        self._client = client
        self._provider = provider
        self._model = model
        self._max_output_tokens = max_output_tokens

    @property
    def provider(self) -> str:
        """Return the application-facing provider name used for diagnostics."""
        return self._provider

    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        """Request and decode one structured JSON response."""
        try:
            with api_call_context(self._provider, "chat-completions"):
                response = await self._client.acompletion(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    response_format=LLMStructuredOutput,
                    temperature=0.2,
                    max_tokens=self._max_output_tokens,
                )
        except AnyLLMError as exc:
            raise LLMError("LLM request failed") from exc
        result = _decode_structured_response(response)
        return result.model_dump(mode="json")


def create_any_llm_provider(
    provider: str,
    model: str,
    max_output_tokens: int,
    http_client: httpx.AsyncClient,
    *,
    api_key: str | None = None,
    api_base: str | None = None,
) -> AnyLLMStructuredProvider:
    """Create an application adapter for any supported any-llm completion provider."""
    provider_class = AnyLLM.get_provider_class(provider)
    if not provider_class.SUPPORTS_COMPLETION:
        raise ValueError(f"any-llm provider does not support completion: {provider}")
    client_options: dict[str, object] = {}
    if issubclass(provider_class, BaseOpenAIProvider):
        client_options.update(http_client=http_client, max_retries=0)
    sdk_client = AnyLLM.create(provider, api_key=api_key, api_base=api_base, **client_options)
    return AnyLLMStructuredProvider(
        sdk_client,
        provider=provider,
        model=model,
        max_output_tokens=max_output_tokens,
    )


def parse_result(
    payload: Mapping[str, Any],
    now: pendulum.DateTime,
    valid_source_ids: set[str],
) -> BriefingResult:
    """Validate an LLM payload and convert it to a briefing result."""
    require_aware_datetime(now, context="Briefing result time")
    structured = _validate_structured_output(payload)

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
