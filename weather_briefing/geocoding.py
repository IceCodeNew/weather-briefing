"""Location geocoding, fallback selection, and local caching."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, replace
from functools import cache
from pathlib import Path
from typing import Any, Protocol

import httpx

from .api_client import api_call_extensions
from .models import LocationResolution, LocationSpec, ResolvedLocation
from .reference_data import ReferenceDataError, reference_string_tuple, reference_value


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


@cache
def _mainland_china_rules() -> tuple[float, float, float, float, frozenset[str]]:
    try:
        latitude = reference_value("geography.json", "mainland_china_service_bounds", "latitude")
        longitude = reference_value("geography.json", "mainland_china_service_bounds", "longitude")
        excluded_areas = reference_string_tuple("geography.json", "mainland_china_excluded_administrative_areas")
        rules = (
            float(latitude["minimum"]),
            float(latitude["maximum"]),
            float(longitude["minimum"]),
            float(longitude["maximum"]),
            frozenset(area.casefold() for area in excluded_areas),
        )
        latitude_min, latitude_max, longitude_min, longitude_max, _ = rules
        if not -90 <= latitude_min < latitude_max <= 90:
            raise ReferenceDataError("Invalid mainland China latitude bounds")
        if not -180 <= longitude_min < longitude_max <= 180:
            raise ReferenceDataError("Invalid mainland China longitude bounds")
        return rules
    except (KeyError, TypeError, ValueError) as exc:
        raise ReferenceDataError("Invalid mainland China geography reference data") from exc


def possibly_mainland_china(latitude: float, longitude: float) -> bool:
    """Return whether coordinates fall inside the broad service bounds."""
    latitude_min, latitude_max, longitude_min, longitude_max, _ = _mainland_china_rules()
    return latitude_min <= latitude <= latitude_max and longitude_min <= longitude <= longitude_max


class OpenMeteoGeocodingProvider:
    """Resolve named locations through the Open-Meteo geocoding API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str = "https://geocoding-api.open-meteo.com",
        api_key: str | None = None,
    ) -> None:
        """Configure Open-Meteo geocoding access and its optional API key."""
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def geocode(self, location: LocationSpec) -> ResolvedLocation:
        """Resolve a location using a matching Open-Meteo result."""
        location_name = _required_location_name(location)
        params: dict[str, str | int] = {
            "name": location_name,
            "count": 5,
            "language": "zh",
            "format": "json",
        }
        if self._api_key:
            params["apikey"] = self._api_key
        try:
            response = await self._client.get(
                f"{self._base_url}/v1/search",
                params=params,
                extensions=api_call_extensions("open-meteo", "geocoding"),
            )
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results", [])
            if not isinstance(results, list) or not results:
                raise GeocodingError(f"Open-Meteo geocoding returned no results for location: {location.id}")
            result = next(
                (
                    item
                    for item in results
                    if isinstance(item, dict) and _open_meteo_result_matches(location_name, item)
                ),
                None,
            )
            if result is None:
                raise GeocodingError(f"Open-Meteo geocoding returned no matching result for location: {location.id}")
            latitude = float(result["latitude"])
            longitude = float(result["longitude"])
            country_code = str(result.get("country_code", "")).upper() or None
            administrative_area = str(result.get("admin1", "")).strip() or None
            timezone = str(result.get("timezone", "")).strip() or None
        except GeocodingError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise GeocodingError(
                f"Open-Meteo geocoding failed for location: {location.id} ({type(exc).__name__})",
                cause_type=type(exc),
            ) from None
        return ResolvedLocation(
            id=location.id,
            name=location_name,
            latitude=latitude,
            longitude=longitude,
            country_code=country_code,
            administrative_area=administrative_area,
            timezone=timezone,
            is_mainland_china=_is_geocoded_mainland(country_code, administrative_area),
            matched_name=str(result.get("name", "")).strip() or location_name,
        )


class NominatimGeocodingProvider:
    """Resolve and reverse-resolve locations through Nominatim."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str,
        base_url: str = "https://nominatim.openstreetmap.org",
    ) -> None:
        """Configure rate-limited Nominatim access with an identifying user agent."""
        if not user_agent.strip():
            raise ValueError("Nominatim requires an identifying User-Agent")
        self._client = client
        self._user_agent = user_agent
        self._base_url = base_url.rstrip("/")
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def geocode(self, location: LocationSpec) -> ResolvedLocation:
        """Resolve a named location while respecting Nominatim rate limits."""
        location_name = _required_location_name(location)
        async with self._lock:
            result: dict[str, object] | None = None
            for query in _nominatim_queries(location_name):
                delay = 1.0 - (time.monotonic() - self._last_request_at)
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    response = await self._client.get(
                        f"{self._base_url}/search",
                        params={
                            "q": query,
                            "format": "jsonv2",
                            "addressdetails": 1,
                            "limit": 5,
                        },
                        headers={"User-Agent": self._user_agent},
                        extensions=api_call_extensions("nominatim", "geocoding"),
                    )
                    self._last_request_at = time.monotonic()
                    response.raise_for_status()
                    results = response.json()
                    if not isinstance(results, list):
                        continue
                    result = next(
                        (
                            item
                            for item in results
                            if isinstance(item, dict) and _nominatim_result_matches(location_name, item)
                        ),
                        None,
                    )
                    if result is not None:
                        break
                except httpx.HTTPError as exc:
                    raise GeocodingError(
                        f"Nominatim geocoding request failed for location: {location.id} ({type(exc).__name__})",
                        cause_type=type(exc),
                    ) from None
            if result is None:
                raise GeocodingError(f"Nominatim geocoding returned no result for location: {location.id}")
            try:
                address = result.get("address", {})
                latitude = float(result["lat"])
                longitude = float(result["lon"])
                country_code = str(address.get("country_code", "")).upper() or None
                administrative_area = str(address.get("state") or address.get("province") or "").strip() or None
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise GeocodingError(
                    "Nominatim geocoding response validation failed "
                    f"for location: {location.id} ({type(exc).__name__})",
                    cause_type=type(exc),
                ) from None
        return ResolvedLocation(
            id=location.id,
            name=location_name,
            latitude=latitude,
            longitude=longitude,
            country_code=country_code,
            administrative_area=administrative_area,
            timezone=None,
            is_mainland_china=_is_geocoded_mainland(country_code, administrative_area),
            matched_name=str(result.get("display_name", "")).strip() or location_name,
        )

    async def reverse_geocode(self, location: LocationSpec) -> ResolvedLocation:
        """Resolve coordinates to the nearest canonical OSM address."""
        if location.latitude is None or location.longitude is None:
            raise GeocodingError(f"Reverse geocoding requires coordinates for location: {location.id}")
        async with self._lock:
            delay = 1.0 - (time.monotonic() - self._last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                response = await self._client.get(
                    f"{self._base_url}/reverse",
                    params={
                        "lat": location.latitude,
                        "lon": location.longitude,
                        "format": "jsonv2",
                        "addressdetails": 1,
                    },
                    headers={"User-Agent": self._user_agent},
                    extensions=api_call_extensions("nominatim", "reverse-geocoding"),
                )
                self._last_request_at = time.monotonic()
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise TypeError("Nominatim reverse response must be an object")
                address = result.get("address", {})
                if not isinstance(address, dict):
                    raise TypeError("Nominatim reverse address must be an object")
                display_name = str(result.get("display_name", "")).strip()
                if not display_name:
                    raise ValueError("Nominatim reverse response has no display name")
                country_code = str(address.get("country_code", "")).upper() or None
                administrative_area = (
                    str(address.get("state") or address.get("province") or address.get("region") or "").strip() or None
                )
            except (httpx.HTTPError, TypeError, ValueError, AttributeError) as exc:
                raise GeocodingError(
                    f"Nominatim reverse geocoding failed for location: {location.id} ({type(exc).__name__})",
                    cause_type=type(exc),
                ) from None
        return ResolvedLocation(
            id=location.id,
            name=display_name,
            latitude=location.latitude,
            longitude=location.longitude,
            country_code=country_code,
            administrative_area=administrative_area,
            timezone=None,
            is_mainland_china=_is_geocoded_mainland(country_code, administrative_area),
            matched_name=display_name,
        )


class FallbackGeocodingProvider:
    """Try geocoding providers in order until one resolves a location."""

    def __init__(self, *providers: GeocodingProvider) -> None:
        """Require and retain providers in fallback priority order."""
        if not providers:
            raise ValueError("At least one geocoding provider is required")
        self._providers = providers

    async def geocode(self, location: LocationSpec) -> ResolvedLocation:
        """Resolve a location with the first successful provider."""
        _required_location_name(location)
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
        location_name = _required_location_name(location)
        try:
            return await self._provider.geocode(location)
        except GeocodingError as direct_error:
            last_error = direct_error
        for candidate_name in _lower_precision_location_names(location_name):
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
        location_name = _required_location_name(location)
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
        cache[cache_key] = _location_cache_record(resolved)
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
        cache[cache_key] = _location_cache_record(resolved)
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


def _location_cache_record(location: ResolvedLocation) -> dict[str, Any]:
    record = asdict(location)
    del record["summary_language"]
    del record["jma_office_code"]
    return record


def _is_geocoded_mainland(country_code: str | None, administrative_area: str | None) -> bool:
    if country_code != "CN":
        return False
    normalized = (administrative_area or "").casefold()
    return normalized not in _mainland_china_rules()[4]


def _required_location_name(location: LocationSpec) -> str:
    name = (location.name or "").strip()
    if not name:
        raise GeocodingError(f"Forward geocoding requires a name for location: {location.id}")
    return name


def _nominatim_queries(name: str) -> tuple[str, ...]:
    normalized = _normalized_location_name(name)
    return tuple(dict.fromkeys((normalized, name)))


def _nominatim_result_matches(name: str, result: dict[str, object]) -> bool:
    display_name = str(result.get("display_name", "")).casefold()
    specific_name = _specific_location_name(name)
    return specific_name.casefold() in display_name


def _open_meteo_result_matches(name: str, result: dict[str, object]) -> bool:
    result_description = " ".join(
        str(result.get(field, "")) for field in ("name", "admin1", "admin2", "admin3", "admin4", "country")
    ).casefold()
    return _specific_location_name(name).casefold() in result_description


def _specific_location_name(name: str) -> str:
    normalized = _normalized_location_name(name)
    suffix_characters = reference_value(
        "geography.json",
        "nominatim_name_rules",
        "mainland_china_administrative_division_suffix_characters",
    )
    if not isinstance(suffix_characters, str) or not suffix_characters:
        raise ReferenceDataError("Nominatim administrative division suffix characters must be a string")
    return re.sub(rf"^.*[{re.escape(suffix_characters)}]", "", normalized).strip() or normalized


def _normalized_location_name(name: str) -> str:
    normalized = name
    for term in reference_string_tuple("geography.json", "nominatim_name_rules", "removable_terms"):
        normalized = normalized.replace(term, "")
    return normalized.strip()


def _lower_precision_location_names(name: str) -> tuple[str, ...]:
    patterns = reference_string_tuple(
        "geography.json",
        "mainland_china_geocoding_precision_reduction_patterns",
    )
    candidates: list[str] = []
    current = name.strip()
    for pattern in patterns:
        reduced = re.sub(pattern, "", current).strip(" ,，")
        if reduced and reduced != name and reduced not in candidates:
            candidates.append(reduced)
        current = reduced
    return tuple(candidates)
