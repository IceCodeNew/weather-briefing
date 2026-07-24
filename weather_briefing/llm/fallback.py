"""Request-failure fallback across two complete LLM providers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from ..notifications import NotificationDecision
from .base import LLMRequestError

_LOGGER = logging.getLogger("weather_briefing.llm")
_Result = TypeVar("_Result")


class CompleteLLMProvider(Protocol):
    """Expose every LLM operation used by application composition."""

    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        """Return one structured briefing response."""
        ...

    async def assess_notification(self, payload: dict[str, object]) -> NotificationDecision:
        """Return whether an official message change merits a notification."""
        ...

    async def translate_service_status(
        self,
        title: str,
        body: str,
        target_language: str,
    ) -> tuple[str, str]:
        """Translate one official service-status message."""
        ...

    async def aclose(self) -> None:
        """Close owned resources."""
        ...


class FallbackLLMProvider:
    """Retry request failures once through a separately configured provider."""

    def __init__(
        self,
        primary: CompleteLLMProvider,
        fallback: CompleteLLMProvider,
        *,
        primary_name: str,
        fallback_name: str,
    ) -> None:
        """Retain provider order and diagnostic names."""
        self._primary = primary
        self._fallback = fallback
        self._primary_name = primary_name
        self._fallback_name = fallback_name
        self._using_fallback = False

    async def _request(
        self,
        operation: str,
        primary_call: Callable[[], Awaitable[_Result]],
        fallback_call: Callable[[], Awaitable[_Result]],
    ) -> _Result:
        if self._using_fallback:
            return await fallback_call()
        try:
            return await primary_call()
        except LLMRequestError:
            self._using_fallback = True
            _LOGGER.warning(
                "Primary LLM request failed; trying fallback operation=%s primary=%s fallback=%s",
                operation,
                self._primary_name,
                self._fallback_name,
            )
            return await fallback_call()

    async def summarize(self, system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
        """Generate a briefing, falling back only after a request failure."""
        return await self._request(
            "summarize",
            lambda: self._primary.summarize(system_prompt, payload),
            lambda: self._fallback.summarize(system_prompt, payload),
        )

    async def assess_notification(self, payload: dict[str, object]) -> NotificationDecision:
        """Assess notification value, falling back only after a request failure."""
        return await self._request(
            "assess-notification",
            lambda: self._primary.assess_notification(payload),
            lambda: self._fallback.assess_notification(payload),
        )

    async def translate_service_status(
        self,
        title: str,
        body: str,
        target_language: str,
    ) -> tuple[str, str]:
        """Translate a status message, falling back only after a request failure."""
        return await self._request(
            "translate-service-status",
            lambda: self._primary.translate_service_status(title, body, target_language),
            lambda: self._fallback.translate_service_status(title, body, target_language),
        )

    async def aclose(self) -> None:
        """Close both providers and preserve every cleanup failure."""
        errors: list[Exception] = []
        try:
            await self._primary.aclose()
        except asyncio.CancelledError:
            try:
                await self._fallback.aclose()
            except Exception as exc:
                _LOGGER.warning(
                    "Failed to close fallback LLM provider during cancellation error_type=%s",
                    type(exc).__name__,
                )
            raise
        except Exception as exc:
            errors.append(exc)
        try:
            await self._fallback.aclose()
        except Exception as exc:
            errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Failed to close fallback LLM providers", errors)
