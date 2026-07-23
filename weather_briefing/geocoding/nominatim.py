"""Nominatim forward and reverse geocoding adapter."""

from __future__ import annotations

import asyncio
import time

import httpx

from ..api_client import api_call_extensions
from ..data.service_endpoints import NOMINATIM_BASE_URL, NOMINATIM_USER_AGENT
from ..models import LocationSpec, ResolvedLocation
from .base import GeocodingError, log_candidate_selection, required_location_name
from .matching import is_geocoded_mainland, nominatim_queries, nominatim_result_matches


class NominatimGeocodingProvider:
    """Resolve and reverse-resolve locations through Nominatim."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str = NOMINATIM_USER_AGENT,
        base_url: str = NOMINATIM_BASE_URL,
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
        location_name = required_location_name(location)
        async with self._lock:
            result: dict[str, object] | None = None
            for query_attempt, query in enumerate(nominatim_queries(location_name), start=1):
                await self._wait_for_rate_limit()
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
                        log_candidate_selection(
                            "nominatim",
                            location.id,
                            query_attempt,
                            0,
                            outcome="invalid-response",
                        )
                        continue
                    result = next(
                        (
                            item
                            for item in results
                            if isinstance(item, dict) and nominatim_result_matches(location_name, item)
                        ),
                        None,
                    )
                    log_candidate_selection(
                        "nominatim",
                        location.id,
                        query_attempt,
                        len(results),
                        outcome="matched" if result is not None else "no-match" if results else "no-results",
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
            is_mainland_china=is_geocoded_mainland(country_code, administrative_area),
            matched_name=str(result.get("display_name", "")).strip() or location_name,
        )

    async def reverse_geocode(self, location: LocationSpec) -> ResolvedLocation:
        """Resolve coordinates to the nearest canonical OSM address."""
        if location.latitude is None or location.longitude is None:
            raise GeocodingError(f"Reverse geocoding requires coordinates for location: {location.id}")
        async with self._lock:
            await self._wait_for_rate_limit()
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
            is_mainland_china=is_geocoded_mainland(country_code, administrative_area),
            matched_name=display_name,
        )

    async def _wait_for_rate_limit(self) -> None:
        delay = 1.0 - (time.monotonic() - self._last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)
