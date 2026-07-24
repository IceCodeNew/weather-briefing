"""Adapter from any-llm to the application LLM contract."""

from __future__ import annotations

import logging
from inspect import isawaitable
from typing import Protocol

from any_llm import AnyLLM
from any_llm.exceptions import AnyLLMError, LengthFinishReasonError
from pydantic import BaseModel

from ..api_client import api_call_context
from .base import LLMRequestError, SensitiveLLMDiagnostics, serialize_llm_payload
from .schema import LLMStructuredOutput, decode_structured_response

_LOGGER = logging.getLogger("weather_briefing.llm")


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
        _LOGGER.debug(
            "LLM request prepared: provider=%s model=%r max_output_tokens=%d",
            self._provider,
            self._model,
            self._max_output_tokens,
        )
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
        except LengthFinishReasonError as exc:
            _LOGGER.warning(
                "LLM response reached output token limit: provider=%s model=%r max_output_tokens=%d error_type=%s",
                self._provider,
                self._model,
                self._max_output_tokens,
                type(exc).__name__,
            )
            raise LLMRequestError("LLM response reached output token limit") from exc
        except AnyLLMError as exc:
            raise LLMRequestError("LLM request failed") from exc
        result_payload = decode_structured_response(response).model_dump(mode="json")
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
