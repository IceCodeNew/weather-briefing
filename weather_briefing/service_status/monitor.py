"""Stateful direct notification orchestration for service-status changes."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import replace
from typing import Protocol

import pendulum

from ..llm import LLMError
from ..state import ServiceStatusState
from .collection import collect_service_status
from .formatting import render_service_status_notification
from .models import ServiceStatusSnapshot
from .statuspage import ServiceStatusProvider

_LOGGER = logging.getLogger("weather_briefing.service_status")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_ASCII_LETTER = re.compile(r"[A-Za-z]")


class ServiceStatusStateStore(Protocol):
    """Persist observed and successfully handled provider states."""

    def service_status_state(self, source_id: str) -> ServiceStatusState | None:
        """Return the last durable state for a provider."""
        ...

    def observe_service_status(
        self,
        source_id: str,
        fingerprint: str,
        unhealthy: bool,
        observed_at: pendulum.DateTime,
    ) -> None:
        """Record an observation before delivery."""
        ...

    def mark_service_status_notified(
        self,
        source_id: str,
        fingerprint: str,
        unhealthy: bool,
        notified_at: pendulum.DateTime,
    ) -> None:
        """Record successful delivery or an intentionally silent baseline."""
        ...


class ServiceStatusTranslator(Protocol):
    """Translate official incident explanations only when English is absent."""

    async def translate_service_status(self, text: str, target_language: str) -> str:
        """Return a faithful localized rendering of one incident explanation."""
        ...


class ServiceStatusDelivery(Protocol):
    """Publish one already formatted status notification."""

    async def publish_alert(self, title: str, body: str) -> None:
        """Deliver a status notification."""
        ...


class ServiceStatusMonitor:
    """Poll official providers and directly publish meaningful state changes."""

    def __init__(
        self,
        providers: tuple[ServiceStatusProvider, ...],
        state: ServiceStatusStateStore,
        delivery: ServiceStatusDelivery,
        translator: ServiceStatusTranslator | None = None,
        language: str = "en",
    ) -> None:
        """Configure independent providers, durable state, and direct delivery."""
        self._providers = providers
        self._state = state
        self._delivery = delivery
        self._translator = translator
        self._language = language

    async def run(self, now: pendulum.DateTime) -> int:
        """Publish changed incidents and recoveries, returning the notification count."""
        snapshots = await collect_service_status(self._providers)
        published = 0
        for snapshot in snapshots:
            if await self._process_snapshot(snapshot, now):
                published += 1
        return published

    async def _process_snapshot(
        self,
        snapshot: ServiceStatusSnapshot,
        now: pendulum.DateTime,
    ) -> bool:
        fingerprint = service_status_fingerprint(snapshot)
        unhealthy = service_status_is_unhealthy(snapshot)
        previous = self._state.service_status_state(snapshot.source_id)
        self._state.observe_service_status(snapshot.source_id, fingerprint, unhealthy, snapshot.observed_at)

        if previous is not None and previous.notified_fingerprint == fingerprint:
            return False
        should_publish = unhealthy or (previous is not None and previous.notified_unhealthy is True)
        if should_publish:
            rendered_snapshot = await self._translate_incidents(snapshot)
            title, body = render_service_status_notification(
                rendered_snapshot,
                recovered=not unhealthy,
                language=self._language,
            )
            await self._delivery.publish_alert(title, body)
        self._state.mark_service_status_notified(snapshot.source_id, fingerprint, unhealthy, now)
        return should_publish

    async def _translate_incidents(self, snapshot: ServiceStatusSnapshot) -> ServiceStatusSnapshot:
        if self._translator is None:
            return snapshot
        incidents = []
        for incident in snapshot.incidents:
            if has_english_explanation(incident.detail):
                incidents.append(incident)
                continue
            try:
                detail = await self._translator.translate_service_status(
                    incident.detail,
                    self._language,
                )
            except LLMError as exc:
                _LOGGER.warning(
                    "Service-status incident translation failed; using the official original text error_type=%s",
                    type(exc).__name__,
                )
                incidents.append(incident)
            else:
                incidents.append(replace(incident, detail=detail))
        return replace(snapshot, incidents=tuple(incidents))


def service_status_is_unhealthy(snapshot: ServiceStatusSnapshot) -> bool:
    """Return whether the official snapshot reports an incident or affected component."""
    return bool(snapshot.incidents) or any(component.status != "operational" for component in snapshot.components)


def service_status_fingerprint(snapshot: ServiceStatusSnapshot) -> str:
    """Hash meaningful state while excluding volatile observation timestamps."""
    payload = {
        "components": sorted(
            (component.name, component.surface, component.status) for component in snapshot.components
        ),
        "incidents": sorted(
            (
                incident.name,
                incident.status,
                incident.impact,
                incident.detail,
                sorted(incident.surfaces),
            )
            for incident in snapshot.incidents
        ),
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode()).hexdigest()


def has_english_explanation(text: str) -> bool:
    """Require enough Latin prose to distinguish English from product-name fragments."""
    letters = _LETTER.findall(text)
    if not letters:
        return False
    ascii_letters = _ASCII_LETTER.findall(text)
    non_ascii_letter_count = len(letters) - len(ascii_letters)
    if non_ascii_letter_count == 0:
        return len(ascii_letters) >= 5
    return len(ascii_letters) >= 20 and len(ascii_letters) / len(letters) >= 0.75
