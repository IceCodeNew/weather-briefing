"""Air-quality providers, normalization, and health guidance."""

from __future__ import annotations

from functools import cache
from typing import Protocol

import httpx
import pendulum

from .api_client import api_call_extensions
from .data.resources import ReferenceDataError, reference_value
from .data.service_endpoints import AQICN_BASE_URL
from .languages import localized_labels
from .localization import localization_table
from .models import AirQualitySnapshot, AirQualityTimeKind, SourceDocument
from .time_utils import parse_datetime_with_default_timezone

_AIR_QUALITY_FORMATS = localization_table("air_quality")


class AirQualityError(RuntimeError):
    """Raised without exposing private API credentials or request URLs."""


class AirQualityProvider(Protocol):
    """Fetch provider-neutral air-quality context for coordinates."""

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        timezone: str,
    ) -> AirQualitySnapshot:
        """Fetch air-quality context for a location and its timezone."""
        ...


class AQICNProvider:
    """Fetch U.S. EPA AQI observations from AQICN."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        token: str,
        base_url: str = AQICN_BASE_URL,
    ) -> None:
        """Configure AQICN access with an injected HTTP client and token."""
        self._client = client
        self._token = token
        self._base_url = base_url.rstrip("/")

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        timezone: str,
    ) -> AirQualitySnapshot:
        """Fetch and normalize the AQICN observation for a location."""
        try:
            response = await self._client.get(
                f"{self._base_url}/feed/geo:{latitude};{longitude}/",
                params={"token": self._token},
                extensions=api_call_extensions("aqicn", "air-quality"),
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "ok":
                raise AirQualityError("AQICN returned a non-success status")
            data = payload["data"]
            aqi = int(data["aqi"])
            pm25_value = data.get("iaqi", {}).get("pm25", {}).get("v")
            pm25_aqi = round(float(pm25_value)) if pm25_value is not None else None
            city = data["city"]
            observed_at = _aqicn_observed_at(data.get("time"), timezone)
        except AirQualityError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise AirQualityError("AQICN request or response validation failed") from None
        category, guidance = health_guidance(aqi)
        return AirQualitySnapshot(
            source_id="air-quality:aqicn",
            source_name=str(city.get("name", "AQICN")),
            source_url=str(city["url"]),
            effective_at=observed_at,
            time_kind=AirQualityTimeKind.OBSERVATION,
            aqi=aqi,
            aqi_display=str(aqi),
            aqi_standard="US EPA",
            pm25_aqi=pm25_aqi,
            pm25_concentration=None,
            pm25_unit=None,
            category=category,
            health_guidance=guidance,
            output_language="en",
        )


def air_quality_to_document(snapshot: AirQualitySnapshot) -> SourceDocument:
    """Convert an air-quality snapshot into a citable source document."""
    labels = localized_labels(snapshot.output_language, _AIR_QUALITY_FORMATS)
    effective_at = (
        snapshot.effective_at.to_iso8601_string() if snapshot.effective_at is not None else labels["unavailable"]
    )
    time_label = (
        labels["forecast_time"] if snapshot.time_kind is AirQualityTimeKind.FORECAST else labels["observation_time"]
    )
    concentration = labels["unavailable"]
    if snapshot.pm25_concentration is not None and snapshot.pm25_unit:
        concentration = f"{snapshot.pm25_concentration:g} {snapshot.pm25_unit}"
    pm25_aqi = labels["unavailable"] if snapshot.pm25_aqi is None else f"{snapshot.pm25_aqi:g}"
    aqi = labels["aqi"].format(
        aqi=snapshot.aqi_display,
        standard=snapshot.aqi_standard,
        category=snapshot.category,
    )
    pm25_index = labels["pm25_aqi"].format(value=pm25_aqi, standard=snapshot.aqi_standard)
    pm25 = labels["pm25"].format(concentration=concentration)
    health = labels["health"].format(guidance=snapshot.health_guidance)
    history_value = "\n".join(
        (
            labels["time_kind"].format(kind=labels[snapshot.time_kind.value]),
            aqi,
            pm25_index,
            pm25,
            health,
        )
    )
    return SourceDocument(
        id=snapshot.source_id,
        name=snapshot.source_name,
        url=snapshot.source_url,
        content="\n".join((f"{time_label}{labels['separator']}{effective_at}", aqi, pm25_index, pm25, health)),
        language=snapshot.output_language,
        history_summary=(
            f"{time_label}{labels['separator']}{effective_at}\n"
            f"{labels['aqi_summary'].format(aqi=snapshot.aqi_display, category=snapshot.category)}\n"
            f"{labels['pm25_summary'].format(value=pm25_aqi, concentration=concentration)}"
        ),
        history_value=history_value,
    )


def health_guidance(aqi: int) -> tuple[str, str]:
    """Return the configured category and health guidance for an AQI."""
    for maximum_aqi, category, guidance in _guidance_bands():
        if maximum_aqi is None or aqi <= maximum_aqi:
            return category, guidance
    raise ReferenceDataError("Air quality guidance must end with an unbounded band")


def _aqicn_observed_at(
    value: object,
    queried_location_timezone: str,
) -> pendulum.DateTime | None:
    if not isinstance(value, dict):
        return None
    time_value = value.get("iso") or value.get("s")
    if not isinstance(time_value, str) or not time_value.strip():
        return None
    response_timezone = value.get("tz")
    default_timezone = (
        response_timezone.strip()
        if isinstance(response_timezone, str) and response_timezone.strip()
        else queried_location_timezone
    )
    return parse_datetime_with_default_timezone(
        time_value,
        default_timezone,
        context="AQICN observation time",
    )


@cache
def _guidance_bands() -> tuple[tuple[int | None, str, str], ...]:
    values = reference_value("air_quality_guidance.json", "bands")
    if not isinstance(values, list) or not values:
        raise ReferenceDataError("Air quality guidance bands must be a non-empty list")
    bands: list[tuple[int | None, str, str]] = []
    try:
        for value in values:
            maximum = value["maximum_aqi"]
            if maximum is not None:
                maximum = int(maximum)
            bands.append((maximum, str(value["category"]), str(value["guidance"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise ReferenceDataError("Invalid air quality guidance band") from exc
    bounded_maxima = [maximum for maximum, _, _ in bands[:-1]]
    if bands[-1][0] is not None or any(maximum is None for maximum in bounded_maxima):
        raise ReferenceDataError("Air quality guidance must end with an unbounded band")
    numeric_maxima = [maximum for maximum in bounded_maxima if maximum is not None]
    if numeric_maxima != sorted(set(numeric_maxima)) or any(maximum < 0 for maximum in numeric_maxima):
        raise ReferenceDataError("Air quality guidance bounds must be unique, increasing, and non-negative")
    return tuple(bands)
