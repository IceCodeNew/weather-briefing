"""LLM provider adapters and structured result validation."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from inspect import isawaitable
from typing import Annotated, Any, Literal, Protocol, TypeAlias

import pendulum
from any_llm import AnyLLM
from any_llm.exceptions import AnyLLMError
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from .api_client import api_call_context
from .models import Advice, AdviceTopic, BriefingResult, Conclusion, Warning
from .time_utils import require_aware_datetime

_LOGGER = logging.getLogger("weather_briefing.llm")


class LLMError(RuntimeError):
    """Raised when the model output violates its contract."""


class LLMRequestError(LLMError):
    """Raised when an LLM request fails independently of the output contract."""


class LLMProvider(Protocol):
    """Produce a structured briefing payload from validated source context."""

    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        """Return one structured model response."""
        ...


def serialize_llm_payload(payload: object) -> str:
    """Serialize an LLM payload consistently for budgeting and transport."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class LLMCompletionClient(Protocol):
    """Expose the any-llm completion operation used by the application adapter."""

    async def acompletion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: type[BaseModel],
        temperature: float,
        max_tokens: int,
    ) -> object:
        """Request one asynchronous structured completion."""
        ...


class SensitiveLLMDiagnostics(Protocol):
    """Expose the runtime switch for application-owned sensitive LLM diagnostics."""

    def rendered_text_logging_enabled(self) -> bool:
        """Return whether temporary sensitive diagnostics are enabled."""
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


def _decode_structured_response(response: object) -> LLMStructuredOutput:
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


class AnyLLMStructuredProvider:
    """Adapt an any-llm provider to the application's structured LLM boundary."""

    def __init__(
        self,
        client: AnyLLM | LLMCompletionClient,
        *,
        provider: str,
        model: str,
        max_output_tokens: int,
        diagnostics: SensitiveLLMDiagnostics | None = None,
        owns_client: bool = False,
    ) -> None:
        """Configure a reusable any-llm client and output limit."""
        self._client = client
        self._provider = provider
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._diagnostics = diagnostics
        self._owns_client = owns_client

    @property
    def provider(self) -> str:
        """Return the application-facing provider name used for diagnostics."""
        return self._provider

    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        """Request and decode one structured JSON response."""
        log_sensitive = _sensitive_llm_diagnostics_enabled(self._diagnostics)
        if log_sensitive:
            _LOGGER.debug(
                "Sensitive LLM request diagnostic: provider=%s model=%s system_prompt=%r payload=%r",
                self._provider,
                self._model,
                system_prompt,
                payload,
            )
        try:
            with api_call_context(self._provider, "chat-completions"):
                response = await self._client.acompletion(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": serialize_llm_payload(payload)},
                    ],
                    response_format=LLMStructuredOutput,
                    temperature=0.2,
                    max_tokens=self._max_output_tokens,
                )
        except AnyLLMError as exc:
            raise LLMRequestError("LLM request failed") from exc
        result = _decode_structured_response(response)
        result_payload = result.model_dump(mode="json")
        if log_sensitive:
            _LOGGER.debug(
                "Sensitive LLM response diagnostic: provider=%s model=%s payload=%r",
                self._provider,
                self._model,
                result_payload,
            )
        return result_payload

    async def aclose(self) -> None:
        """Close transports owned by an any-llm client created by this adapter."""
        if not self._owns_client:
            return
        if await _close_llm_resource(self._client):
            return
        client_attributes = getattr(self._client, "__dict__", None)
        if not isinstance(client_attributes, dict):
            _LOGGER.debug(
                "LLM SDK resource has no discoverable nested resources type=%s",
                type(self._client).__name__,
            )
            return
        seen = {id(self._client)}
        for resource in client_attributes.values():
            if id(resource) in seen:
                continue
            seen.add(id(resource))
            await _close_llm_resource(resource)


async def _close_llm_resource(resource: object) -> bool:
    """Close one SDK resource without replacing a task failure during cleanup."""
    close = getattr(resource, "aclose", None)
    if not callable(close):
        close = getattr(resource, "close", None)
    if not callable(close):
        return False
    try:
        result = close()
        if isawaitable(result):
            await result
    except Exception as exc:
        _LOGGER.warning(
            "Failed to close LLM SDK resource type=%s error_type=%s",
            type(resource).__name__,
            type(exc).__name__,
        )
        return False
    return True


def _sensitive_llm_diagnostics_enabled(diagnostics: SensitiveLLMDiagnostics | None) -> bool:
    """Check the runtime switch without letting diagnostic failures affect requests."""
    if diagnostics is None or not _LOGGER.isEnabledFor(logging.DEBUG):
        return False
    try:
        return diagnostics.rendered_text_logging_enabled()
    except Exception:
        _LOGGER.warning("Sensitive LLM diagnostic state check failed", exc_info=True)
        return False


def create_any_llm_provider(
    provider: str,
    model: str,
    max_output_tokens: int,
    *,
    api_key: str | None = None,
    api_base: str | None = None,
    diagnostics: SensitiveLLMDiagnostics | None = None,
) -> AnyLLMStructuredProvider:
    """Create an application adapter for any supported any-llm completion provider."""
    provider_class = AnyLLM.get_provider_class(provider)
    if not provider_class.SUPPORTS_COMPLETION:
        raise ValueError(f"any-llm provider does not support completion: {provider}")
    sdk_client = AnyLLM.create(provider, api_key=api_key, api_base=api_base)
    return AnyLLMStructuredProvider(
        sdk_client,
        provider=provider,
        model=model,
        max_output_tokens=max_output_tokens,
        diagnostics=diagnostics,
        owns_client=True,
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
