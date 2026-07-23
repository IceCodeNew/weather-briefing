"""Application geocoding contracts and safe errors."""

from __future__ import annotations

import logging
from typing import Protocol

from ..models import LocationSpec, ResolvedLocation

_LOGGER = logging.getLogger("weather_briefing.geocoding")


def log_candidate_selection(
    provider: str,
    location_id: str,
    query_attempt: int,
    candidate_count: int,
    *,
    outcome: str,
) -> None:
    """Log candidate-selection metadata without exposing provider payloads."""
    _LOGGER.info(
        "Geocoding candidate selection provider=%s location_id=%s query_attempt=%d candidate_count=%d outcome=%s",
        provider,
        location_id,
        query_attempt,
        candidate_count,
        outcome,
    )


class GeocodingError(RuntimeError):
    """Raised when a configured place cannot be resolved safely."""

    def __init__(self, message: str, *, cause_type: type[Exception] | None = None) -> None:
        """Retain a safe exception class without preserving sensitive error text."""
        super().__init__(message)
        self.cause_type = cause_type


class GeocodingProvider(Protocol):
    """Resolve a named location to coordinates and geographic metadata."""

    async def geocode(self, location: LocationSpec) -> ResolvedLocation:
        """Resolve one named location."""
        ...


class ReverseGeocodingProvider(Protocol):
    """Resolve coordinates to a canonical location name and metadata."""

    async def reverse_geocode(self, location: LocationSpec) -> ResolvedLocation:
        """Reverse-geocode one coordinate-bearing location."""
        ...


def required_location_name(location: LocationSpec) -> str:
    """Return a normalized required name for forward geocoding."""
    name = (location.name or "").strip()
    if not name:
        raise GeocodingError(f"Forward geocoding requires a name for location: {location.id}")
    return name
