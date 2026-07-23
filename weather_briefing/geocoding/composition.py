"""Geocoding fallback and precision-reduction composition."""

from __future__ import annotations

from dataclasses import replace

from ..models import LocationSpec, ResolvedLocation
from .base import GeocodingError, GeocodingProvider, required_location_name
from .matching import lower_precision_location_names


class FallbackGeocodingProvider:
    """Try geocoding providers in order until one resolves a location."""

    def __init__(self, *providers: GeocodingProvider) -> None:
        """Require and retain providers in fallback priority order."""
        if not providers:
            raise ValueError("At least one geocoding provider is required")
        self._providers = providers

    async def geocode(self, location: LocationSpec) -> ResolvedLocation:
        """Resolve a location with the first successful provider."""
        required_location_name(location)
        errors: list[GeocodingError] = []
        for provider in self._providers:
            try:
                return await provider.geocode(location)
            except GeocodingError as exc:
                errors.append(exc)
        last_cause_type = errors[-1].cause_type
        detail = f" ({last_cause_type.__name__})" if last_cause_type is not None else ""
        raise GeocodingError(
            f"No geocoder could resolve location: {location.id}{detail}",
            cause_type=last_cause_type,
        ) from None


class PrecisionReducingGeocodingProvider:
    """Retry failed Chinese place names at progressively lower precision."""

    def __init__(self, provider: GeocodingProvider) -> None:
        """Wrap a geocoder with progressively broader Chinese-name retries."""
        self._provider = provider

    async def geocode(self, location: LocationSpec) -> ResolvedLocation:
        """Resolve a location directly before trying safe broader names."""
        location_name = required_location_name(location)
        try:
            return await self._provider.geocode(location)
        except GeocodingError as direct_error:
            last_error = direct_error
        for candidate_name in lower_precision_location_names(location_name):
            candidate = replace(location, name=candidate_name)
            try:
                resolved = await self._provider.geocode(candidate)
            except GeocodingError as exc:
                last_error = exc
                continue
            return replace(
                resolved,
                name=location_name,
                precision_reduced=True,
            )
        detail = f" ({last_error.cause_type.__name__})" if last_error.cause_type is not None else ""
        raise GeocodingError(
            f"No geocoder could resolve location at a safe precision: {location.id}{detail}",
            cause_type=last_error.cause_type,
        ) from None
