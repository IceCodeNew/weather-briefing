"""Location resolution and local geocoding cache."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ..models import LocationResolution, LocationSpec, ResolvedLocation
from .base import GeocodingError, GeocodingProvider, ReverseGeocodingProvider, required_location_name
from .matching import possibly_mainland_china


class CachedLocationResolver:
    """Resolve complete locations while caching provider-derived metadata."""

    def __init__(
        self,
        provider: GeocodingProvider,
        cache_path: Path,
        *,
        reverse_provider: ReverseGeocodingProvider | None = None,
    ) -> None:
        """Configure forward and optional reverse geocoding with a local cache."""
        self._provider = provider
        self._cache_path = cache_path
        self._reverse_provider = reverse_provider

    async def resolve(self, location: LocationSpec) -> ResolvedLocation:
        """Resolve a location and return its normalized value."""
        return (await self.resolve_with_metadata(location)).location

    async def resolve_with_metadata(self, location: LocationSpec) -> LocationResolution:
        """Resolve a location and report whether its result came from cache."""
        location_name = (location.name or "").strip() or None
        if location.latitude is not None and location.longitude is not None:
            if location_name is None:
                return await self._reverse_geocode(location, location.latitude, location.longitude)
            return LocationResolution(
                ResolvedLocation(
                    id=location.id,
                    name=location_name,
                    latitude=location.latitude,
                    longitude=location.longitude,
                    country_code=None,
                    administrative_area=None,
                    timezone=None,
                    is_mainland_china=possibly_mainland_china(location.latitude, location.longitude),
                    summary_language=location.summary_language,
                    jma_office_code=location.jma_office_code,
                ),
                from_cache=False,
            )
        cache = self._read_cache()
        location_name = required_location_name(location)
        cache_key = f"{location.id}:{location_name}"
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            try:
                return LocationResolution(
                    replace(
                        ResolvedLocation(**cached),
                        summary_language=location.summary_language,
                        jma_office_code=location.jma_office_code,
                    ),
                    from_cache=True,
                )
            except (TypeError, ValueError) as exc:
                raise GeocodingError(
                    f"Invalid cached geocoding record for location: {location.id} ({type(exc).__name__})",
                    cause_type=type(exc),
                ) from None
        if cached is not None:
            raise GeocodingError(f"Invalid cached geocoding record for location: {location.id}")
        resolved = replace(
            await self._provider.geocode(location),
            summary_language=location.summary_language,
            jma_office_code=location.jma_office_code,
        )
        cache[cache_key] = location_cache_record(resolved)
        self._write_cache(cache)
        return LocationResolution(resolved, from_cache=False)

    async def _reverse_geocode(
        self,
        location: LocationSpec,
        latitude: float,
        longitude: float,
    ) -> LocationResolution:
        if self._reverse_provider is None:
            raise GeocodingError(f"No reverse geocoder configured for location: {location.id}")
        cache = self._read_cache()
        cache_key = f"{location.id}:coords:{latitude:.7f},{longitude:.7f}"
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            try:
                return LocationResolution(
                    replace(
                        ResolvedLocation(**cached),
                        summary_language=location.summary_language,
                        jma_office_code=location.jma_office_code,
                    ),
                    from_cache=True,
                )
            except (TypeError, ValueError) as exc:
                raise GeocodingError(
                    f"Invalid cached reverse geocoding record for location: {location.id} ({type(exc).__name__})",
                    cause_type=type(exc),
                ) from None
        if cached is not None:
            raise GeocodingError(f"Invalid cached reverse geocoding record for location: {location.id}")
        resolved = replace(
            await self._reverse_provider.reverse_geocode(location),
            summary_language=location.summary_language,
            jma_office_code=location.jma_office_code,
        )
        cache[cache_key] = location_cache_record(resolved)
        self._write_cache(cache)
        return LocationResolution(resolved, from_cache=False)

    def _read_cache(self) -> dict[str, Any]:
        if not self._cache_path.exists():
            return {}
        try:
            value = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GeocodingError("Geocoding cache must contain readable JSON") from exc
        if not isinstance(value, dict):
            raise GeocodingError("Geocoding cache root must be an object")
        return value

    def _write_cache(self, cache: dict[str, Any]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._cache_path.with_suffix(f"{self._cache_path.suffix}.tmp")
        temporary.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self._cache_path)


def location_cache_record(location: ResolvedLocation) -> dict[str, Any]:
    """Serialize stable provider-derived location fields for caching."""
    record = asdict(location)
    del record["summary_language"]
    del record["jma_office_code"]
    return record
