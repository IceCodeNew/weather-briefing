"""Lazy LLM boundary for service-status decisions and translations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..notifications import NotificationDecision


class ServiceStatusLLM(Protocol):
    """Provide the LLM operations used by service-status monitoring."""

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


class LazyServiceStatusLLM:
    """Create the configured LLM only when a decision or translation is required."""

    def __init__(self, factory: Callable[[], ServiceStatusLLM]) -> None:
        """Store a synchronous provider factory without invoking it."""
        self._factory = factory
        self._provider: ServiceStatusLLM | None = None

    def _get(self) -> ServiceStatusLLM:
        if self._provider is None:
            self._provider = self._factory()
        return self._provider

    async def assess_notification(self, payload: dict[str, object]) -> NotificationDecision:
        """Lazily evaluate whether a change merits a notification."""
        return await self._get().assess_notification(payload)

    async def translate_service_status(
        self,
        title: str,
        body: str,
        target_language: str,
    ) -> tuple[str, str]:
        """Lazily translate an official service-status message."""
        return await self._get().translate_service_status(title, body, target_language)

    async def aclose(self) -> None:
        """Close the real provider only when it was created."""
        if self._provider is not None:
            await self._provider.aclose()
