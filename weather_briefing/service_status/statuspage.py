"""Strict adapter for official Statuspage-compatible summary APIs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, TypeGuard

import httpx

from ..time_utils import parse_aware_datetime
from .models import (
    ServiceComponentStatus,
    ServiceIncident,
    ServiceStatusSnapshot,
    ServiceSurface,
)


class ServiceStatusError(RuntimeError):
    """Report an unavailable or invalid official service-status response."""


class ServiceStatusProvider(Protocol):
    """Fetch one provider-neutral service-status snapshot."""

    async def fetch(self) -> ServiceStatusSnapshot:
        """Return the current official status."""
        ...


class StatuspageProvider:
    """Convert an official Statuspage summary response into domain models."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        provider_id: str,
        provider_name: str,
        api_url: str,
        page_url: str,
        classify_component: Callable[[str], ServiceSurface],
    ) -> None:
        """Configure the official endpoint and provider-specific classifier."""
        self._client = client
        self._provider_id = provider_id
        self._provider_name = provider_name
        self._api_url = api_url
        self._page_url = page_url
        self._classify_component = classify_component

    async def fetch(self) -> ServiceStatusSnapshot:
        """Fetch and strictly validate one Statuspage summary."""
        try:
            response = await self._client.get(self._api_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServiceStatusError(f"{self._provider_name} status request failed") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ServiceStatusError(f"{self._provider_name} status response is not valid JSON") from exc
        if not _is_string_object_mapping(payload):
            raise ServiceStatusError(f"{self._provider_name} status response must be an object")
        page = _required_mapping(payload, "page", self._provider_name)
        observed_at = parse_aware_datetime(
            _required_string(page, "updated_at", self._provider_name),
            context=f"{self._provider_name} status update time",
        )
        components = self._parse_components(payload)
        incidents = self._parse_incidents(payload)
        return ServiceStatusSnapshot(
            source_id=f"service-status:{self._provider_id}",
            source_name=f"{self._provider_name} Status",
            source_url=self._page_url,
            observed_at=observed_at,
            components=components,
            incidents=incidents,
        )

    def _parse_components(self, payload: Mapping[str, object]) -> tuple[ServiceComponentStatus, ...]:
        values = _required_list(payload, "components", self._provider_name)
        components: list[ServiceComponentStatus] = []
        for value in values:
            if not _is_string_object_mapping(value):
                raise ServiceStatusError(f"{self._provider_name} status component must be an object")
            if value.get("group") is True:
                continue
            name = _required_string(value, "name", self._provider_name)
            components.append(
                ServiceComponentStatus(
                    name=name,
                    surface=self._classify_component(name),
                    status=_required_string(value, "status", self._provider_name),
                    updated_at=parse_aware_datetime(
                        _required_string(value, "updated_at", self._provider_name),
                        context=f"{self._provider_name} component update time",
                    ),
                )
            )
        if not components:
            raise ServiceStatusError(f"{self._provider_name} status response has no service components")
        return tuple(components)

    def _parse_incidents(self, payload: Mapping[str, object]) -> tuple[ServiceIncident, ...]:
        values = _required_list(payload, "incidents", self._provider_name)
        return tuple(self._parse_incident(value) for value in values)

    def _parse_incident(self, value: object) -> ServiceIncident:
        if not _is_string_object_mapping(value):
            raise ServiceStatusError(f"{self._provider_name} status incident must be an object")
        updates = _required_list(value, "incident_updates", self._provider_name)
        if not updates or not _is_string_object_mapping(updates[0]):
            raise ServiceStatusError(f"{self._provider_name} status incident has no valid update")
        latest = updates[0]
        affected_names = _incident_component_names(value, latest, self._provider_name)
        surfaces = tuple(dict.fromkeys(self._classify_component(name) for name in affected_names))
        return ServiceIncident(
            name=_required_string(value, "name", self._provider_name),
            status=_required_string(value, "status", self._provider_name),
            impact=_required_string(value, "impact", self._provider_name),
            updated_at=parse_aware_datetime(
                _required_string(value, "updated_at", self._provider_name),
                context=f"{self._provider_name} incident update time",
            ),
            detail=_required_string(latest, "body", self._provider_name),
            surfaces=surfaces,
        )


def _incident_component_names(
    incident: Mapping[str, object],
    latest: Mapping[str, object],
    provider_name: str,
) -> tuple[str, ...]:
    names: list[str] = []
    for owner, field in ((incident, "components"), (latest, "affected_components")):
        values = owner.get(field)
        if values is None:
            continue
        if not isinstance(values, list):
            raise ServiceStatusError(f"{provider_name} incident {field} must be an array or null")
        for value in values:
            if not _is_string_object_mapping(value):
                raise ServiceStatusError(f"{provider_name} incident component must be an object")
            names.append(_required_string(value, "name", provider_name))
    return tuple(dict.fromkeys(names))


def _required_mapping(
    owner: Mapping[str, object],
    field: str,
    provider_name: str,
) -> Mapping[str, object]:
    value = owner.get(field)
    if not _is_string_object_mapping(value):
        raise ServiceStatusError(f"{provider_name} status field {field} must be an object")
    return value


def _required_list(owner: Mapping[str, object], field: str, provider_name: str) -> list[object]:
    value = owner.get(field)
    if not _is_object_list(value):
        raise ServiceStatusError(f"{provider_name} status field {field} must be an array")
    return value


def _required_string(owner: Mapping[str, object], field: str, provider_name: str) -> str:
    value = owner.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ServiceStatusError(f"{provider_name} status field {field} must be a non-empty string")
    return value


def _is_string_object_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)
