"""Open-Meteo geocoding adapter."""

from __future__ import annotations

import httpx

from ..api_client import api_call_extensions
from ..data.service_endpoints import OPEN_METEO_GEOCODING_BASE_URL
from ..models import LocationSpec, ResolvedLocation
from .base import GeocodingError, log_candidate_selection, required_location_name
from .matching import is_geocoded_mainland, open_meteo_result_matches


class OpenMeteoGeocodingProvider:
    """Resolve named locations through the Open-Meteo geocoding API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str = OPEN_METEO_GEOCODING_BASE_URL,
        api_key: str | None = None,
    ) -> None:
        """Configure Open-Meteo geocoding access and its optional API key."""
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def geocode(self, location: LocationSpec) -> ResolvedLocation:
        """Resolve a location using a matching Open-Meteo result."""
        location_name = required_location_name(location)
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
            if not isinstance(results, list):
                log_candidate_selection("open-meteo", location.id, 1, 0, outcome="invalid-response")
                raise GeocodingError(f"Open-Meteo geocoding returned an invalid response for location: {location.id}")
            if not results:
                log_candidate_selection("open-meteo", location.id, 1, 0, outcome="no-results")
                raise GeocodingError(f"Open-Meteo geocoding returned no results for location: {location.id}")
            result = next(
                (item for item in results if isinstance(item, dict) and open_meteo_result_matches(location_name, item)),
                None,
            )
            log_candidate_selection(
                "open-meteo",
                location.id,
                1,
                len(results),
                outcome="matched" if result is not None else "no-match",
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
            is_mainland_china=is_geocoded_mainland(country_code, administrative_area),
            matched_name=str(result.get("name", "")).strip() or location_name,
        )
