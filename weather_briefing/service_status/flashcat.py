"""Strict adapter for structured status data embedded by Flashcat pages."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import TypeGuard

import httpx
import pendulum
from bs4 import BeautifulSoup

from ..api_client import api_call_extensions
from .models import (
    ServiceComponentStatus,
    ServiceIncident,
    ServiceStatusSnapshot,
    ServiceSurface,
)
from .statuspage import ServiceStatusError


class FlashcatStatusProvider:
    """Convert a Flashcat page's server-rendered status snapshot."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        provider_id: str,
        provider_name: str,
        page_url: str,
        classify_component: Callable[[str], ServiceSurface],
    ) -> None:
        """Configure the official page and provider-specific classifier."""
        self._client = client
        self._provider_id = provider_id
        self._provider_name = provider_name
        self._page_url = page_url
        self._classify_component = classify_component

    async def fetch(self) -> ServiceStatusSnapshot:
        """Fetch and strictly validate the server-rendered status snapshot."""
        try:
            response = await self._client.get(
                self._page_url,
                extensions=api_call_extensions(self._provider_id, "status-page"),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServiceStatusError(f"{self._provider_name} status request failed") from exc
        props = _snapshot_props(response.text, self._provider_name)
        initial_data = _required_mapping(props, "initialData", self._provider_name)
        page = _required_mapping(initial_data, "page", self._provider_name)
        active_changes = _required_list(initial_data, "active_changes", self._provider_name)
        observed_at_ms = props.get("initialDataUpdatedAt")
        if not isinstance(observed_at_ms, int) or observed_at_ms <= 0:
            raise ServiceStatusError(
                f"{self._provider_name} embedded field initialDataUpdatedAt must be a positive integer"
            )
        status_by_component = _active_component_statuses(active_changes, self._provider_name)
        components = self._parse_components(page, status_by_component, observed_at_ms)
        incidents = tuple(self._parse_incident(value) for value in active_changes)
        return ServiceStatusSnapshot(
            source_id=f"service-status:{self._provider_id}",
            source_name=f"{self._provider_name} Status",
            source_url=self._page_url,
            observed_at=pendulum.from_timestamp(observed_at_ms / 1000, tz="UTC"),
            components=components,
            incidents=incidents,
        )

    def _parse_components(
        self,
        page: Mapping[str, object],
        status_by_component: Mapping[str, str],
        observed_at_ms: int,
    ) -> tuple[ServiceComponentStatus, ...]:
        values = _required_list(page, "components", self._provider_name)
        components: list[ServiceComponentStatus] = []
        for value in values:
            if not _is_string_object_mapping(value):
                raise ServiceStatusError(f"{self._provider_name} embedded component must be an object")
            if value.get("hide_all") is True:
                continue
            component_id = _required_string(value, "component_id", self._provider_name)
            name = _required_string(value, "name", self._provider_name)
            components.append(
                ServiceComponentStatus(
                    name=name,
                    surface=self._classify_component(name),
                    status=status_by_component.get(component_id, "operational"),
                    updated_at=pendulum.from_timestamp(observed_at_ms / 1000, tz="UTC"),
                )
            )
        if not components:
            raise ServiceStatusError(f"{self._provider_name} embedded status has no visible components")
        return tuple(components)

    def _parse_incident(self, value: object) -> ServiceIncident:
        value = _required_object(value, f"{self._provider_name} active change")
        affected_components = _required_list(value, "affected_components", self._provider_name)
        component_names: list[str] = []
        component_statuses: list[str] = []
        for component in affected_components:
            component = _required_object(component, f"{self._provider_name} affected component")
            component_names.append(_required_string(component, "name", self._provider_name))
            component_statuses.append(_required_string(component, "status", self._provider_name))
        updates = _required_list(value, "updates", self._provider_name)
        parsed_updates = tuple(_parse_update(update, self._provider_name) for update in updates)
        if not parsed_updates:
            raise ServiceStatusError(f"{self._provider_name} active change has no valid update")
        latest_at, latest_detail = max(parsed_updates, key=lambda update: update[0])
        surfaces = tuple(dict.fromkeys(self._classify_component(name) for name in component_names))
        return ServiceIncident(
            name=_required_string(value, "title", self._provider_name),
            status=_required_string(value, "status", self._provider_name),
            impact=_greatest_impact(component_statuses),
            updated_at=pendulum.from_timestamp(latest_at, tz="UTC"),
            detail=latest_detail,
            surfaces=surfaces,
        )


def _snapshot_props(html: str, provider_name: str) -> Mapping[str, object]:
    for script in BeautifulSoup(html, "html.parser").find_all("script"):
        script_text = script.string
        if not script_text or not script_text.startswith("self.__next_f.push("):
            continue
        argument = script_text.removeprefix("self.__next_f.push(").removesuffix(")")
        try:
            flight_item = json.loads(argument)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(flight_item, list)
            or len(flight_item) != 2
            or flight_item[0] != 1
            or not isinstance(flight_item[1], str)
        ):
            continue
        _, separator, serialized_tree = flight_item[1].partition(":")
        if not separator or '"active_changes"' not in serialized_tree:
            continue
        try:
            tree = json.loads(serialized_tree)
        except json.JSONDecodeError:
            continue
        props = _find_snapshot_props(tree)
        if props is not None:
            return props
    raise ServiceStatusError(f"{provider_name} page has no valid embedded status snapshot")


def _find_snapshot_props(value: object) -> Mapping[str, object] | None:
    if _is_string_object_mapping(value):
        initial_data = value.get("initialData")
        if (
            _is_string_object_mapping(initial_data)
            and "page" in initial_data
            and "active_changes" in initial_data
            and "initialDataUpdatedAt" in value
        ):
            return value
        for item in value.values():
            if found := _find_snapshot_props(item):
                return found
    elif isinstance(value, list):
        for item in value:
            if found := _find_snapshot_props(item):
                return found
    return None


def _active_component_statuses(
    active_changes: list[object],
    provider_name: str,
) -> Mapping[str, str]:
    statuses: dict[str, str] = {}
    for change in active_changes:
        change = _required_object(change, f"{provider_name} active change")
        for component in _required_list(change, "affected_components", provider_name):
            component = _required_object(component, f"{provider_name} affected component")
            statuses[_required_string(component, "component_id", provider_name)] = _required_string(
                component,
                "status",
                provider_name,
            )
    return statuses


def _parse_update(value: object, provider_name: str) -> tuple[int, str]:
    if not _is_string_object_mapping(value):
        raise ServiceStatusError(f"{provider_name} incident update must be an object")
    at_seconds = value.get("at_seconds")
    if not isinstance(at_seconds, int) or at_seconds <= 0:
        raise ServiceStatusError(f"{provider_name} incident update time must be a positive integer")
    description = value.get("description")
    if not isinstance(description, str) or not description.strip():
        description = value.get("message")
    if not isinstance(description, str) or not description.strip():
        raise ServiceStatusError(f"{provider_name} incident update must have a non-empty description")
    return at_seconds, description


def _greatest_impact(statuses: list[str]) -> str:
    rank = {
        "operational": 0,
        "maintenance": 1,
        "degraded": 2,
        "degraded_performance": 2,
        "partial_outage": 3,
        "full_outage": 4,
        "major_outage": 4,
    }
    return max(statuses, key=lambda status: rank.get(status, 5), default="unknown")


def _required_mapping(
    owner: Mapping[str, object],
    field: str,
    provider_name: str,
) -> Mapping[str, object]:
    value = owner.get(field)
    if not _is_string_object_mapping(value):
        raise ServiceStatusError(f"{provider_name} embedded field {field} must be an object")
    return value


def _required_object(value: object, context: str) -> Mapping[str, object]:
    if not _is_string_object_mapping(value):
        raise ServiceStatusError(f"{context} must be an object")
    return value


def _required_list(owner: Mapping[str, object], field: str, provider_name: str) -> list[object]:
    value = owner.get(field)
    if not _is_object_list(value):
        raise ServiceStatusError(f"{provider_name} embedded field {field} must be an array")
    return value


def _required_string(owner: Mapping[str, object], field: str, provider_name: str) -> str:
    value = owner.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ServiceStatusError(f"{provider_name} embedded field {field} must be a non-empty string")
    return value


def _is_string_object_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)
