"""Open-Meteo weather, air-quality, and allergen adapter."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from weather_briefing import allergen as allergen_module
from weather_briefing.api_client import api_call_extensions
from weather_briefing.data.resources import ReferenceDataError
from weather_briefing.data.service_endpoints import OPEN_METEO_AIR_QUALITY_BASE_URL, OPEN_METEO_WEATHER_BASE_URL
from weather_briefing.languages import LanguageSupport
from weather_briefing.models import AirQualitySnapshot, AirQualityTimeKind, AllergenSnapshot, WeatherContextSnapshot
from weather_briefing.time_utils import parse_datetime_with_default_timezone

from . import open_meteo_parsing, open_meteo_reference
from .base import WeatherContextError, _is_string_keyed_dict, _safe_provider_error

if TYPE_CHECKING:
    import pendulum

_LOGGER = logging.getLogger("weather_briefing.weather_context")
OPEN_METEO_LANGUAGE_SUPPORT = LanguageSupport.fixed("en")
open_meteo_reference.open_meteo_weather_code_descriptions()


class OpenMeteoProvider:
    """Fetch global weather, air-quality, and pollen context from Open-Meteo."""

    language_support = OPEN_METEO_LANGUAGE_SUPPORT

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        weather_base_url: str = OPEN_METEO_WEATHER_BASE_URL,
        air_quality_base_url: str = OPEN_METEO_AIR_QUALITY_BASE_URL,
        api_key: str | None = None,
    ) -> None:
        """Configure Open-Meteo weather and air-quality endpoints."""
        self._client = client
        self._weather_base_url = weather_base_url
        self._air_quality_base_url = air_quality_base_url
        self._api_key = api_key

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None = None,
    ) -> WeatherContextSnapshot:
        """Fetch and normalize Open-Meteo context for a location."""
        params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": ",".join(  # noqa: FLY002
                (
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "apparent_temperature_max",
                    "apparent_temperature_min",
                    "precipitation_sum",
                    "precipitation_probability_max",
                    "wind_speed_10m_max",
                    "wind_gusts_10m_max",
                    "wind_direction_10m_dominant",
                    "uv_index_max",
                )
            ),
            "current": "relative_humidity_2m",
            "timezone": "auto",
        }
        if forecast_date is None:
            params["forecast_days"] = 2
        else:
            params["start_date"] = str(forecast_date)
            params["end_date"] = str(forecast_date)
        if self._api_key:
            params["apikey"] = self._api_key
        try:
            response = await self._client.get(
                f"{self._weather_base_url}/v1/forecast",
                params=params,
                extensions=api_call_extensions("open-meteo", "weather-forecast"),
            )
            response.raise_for_status()
            payload = response.json()
            daily = payload["daily"]
            if not _is_string_keyed_dict(daily):
                msg = "daily forecast must be an object"
                raise open_meteo_parsing.OpenMeteoResponseError(msg)  # noqa: TRY301
            times = open_meteo_parsing.daily_values(daily, "time")
            forecast_count = min(2, len(times)) if forecast_date is None else len(times)
            weather_forecast = tuple(open_meteo_parsing.format_day(daily, index) for index in range(forecast_count))
            if not weather_forecast:
                msg = "Open-Meteo returned no daily forecast"
                raise WeatherContextError(msg)  # noqa: TRY301
            current: dict[str, object] = payload["current"]
            observed_at = parse_datetime_with_default_timezone(
                str(current["time"]),
                str(payload["timezone"]),
                context="Open-Meteo weather update time",
            )
        except WeatherContextError:
            raise
        except open_meteo_parsing.OpenMeteoResponseError as exc:
            msg = f"Open-Meteo weather forecast parsing failed: {exc}"
            raise WeatherContextError(msg) from None
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            msg = f"Open-Meteo weather forecast failed: {_safe_provider_error(exc)}"
            raise WeatherContextError(msg) from None

        air_quality, allergen = await self._fetch_air_quality_and_allergen(
            latitude,
            longitude,
            forecast_date=forecast_date,
        )
        return WeatherContextSnapshot(
            source_id="weather:open-meteo",
            source_name="Open-Meteo",
            source_url="https://open-meteo.com/",
            observed_at=observed_at,
            weather_forecast=weather_forecast,
            air_quality=air_quality,
            allergen=allergen,
            output_language=OPEN_METEO_LANGUAGE_SUPPORT.default,
        )

    async def fetch_for_date(
        self,
        latitude: float,
        longitude: float,
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot:
        """Fetch Open-Meteo context for an explicit forecast date."""
        return await self.fetch(latitude, longitude, forecast_date=forecast_date)

    async def _fetch_air_quality_and_allergen(  # noqa: C901
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None,
    ) -> tuple[AirQualitySnapshot | None, AllergenSnapshot | None]:
        try:
            pollen_types = allergen_module.pollen_type_names()
        except ReferenceDataError as exc:
            _LOGGER.warning(
                "Weather API optional enrichment failed provider=open-meteo operation=allergen reason=%s",
                type(exc).__name__,
            )
            pollen_types = ()
        variables = (
            "us_aqi",
            "us_aqi_pm2_5",
            "pm2_5",
            *(f"{key}_pollen" for key, _ in pollen_types),
        )
        params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": "auto",
        }
        if forecast_date is None:
            params["current"] = ",".join(variables)
        else:
            params["hourly"] = ",".join(variables)
            params["start_date"] = str(forecast_date)
            params["end_date"] = str(forecast_date)
        if self._api_key:
            params["apikey"] = self._api_key
        try:
            response = await self._client.get(
                f"{self._air_quality_base_url}/v1/air-quality",
                params=params,
                extensions=api_call_extensions("open-meteo", "air-quality"),
            )
            response.raise_for_status()
            payload = response.json()
            if not _is_string_keyed_dict(payload):
                msg = "air-quality response must be an object"
                raise open_meteo_parsing.OpenMeteoResponseError(msg)
            if forecast_date is None:
                air_quality_values = payload["current"]
                if not _is_string_keyed_dict(air_quality_values):
                    msg = "current air quality must be an object"
                    raise open_meteo_parsing.OpenMeteoResponseError(msg)
                allergen_values = air_quality_values
            else:
                hourly = payload["hourly"]
                if not _is_string_keyed_dict(hourly):
                    msg = "hourly air quality must be an object"
                    raise open_meteo_parsing.OpenMeteoResponseError(msg)
                air_quality_values, allergen_values = open_meteo_parsing.daily_peak_values(hourly, pollen_types)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            reason = (
                str(exc) if isinstance(exc, open_meteo_parsing.OpenMeteoResponseError) else _safe_provider_error(exc)
            )
            _LOGGER.warning(
                "Weather API optional call failed provider=open-meteo operation=air-quality reason=%s",
                reason,
            )
            return None, None
        allergen = None
        if pollen_types:
            try:
                allergen = open_meteo_parsing.parse_allergen(allergen_values, payload, pollen_types)
            except ReferenceDataError as exc:
                _LOGGER.warning(
                    "Weather API optional enrichment failed provider=open-meteo operation=allergen reason=%s",
                    type(exc).__name__,
                )
        time_kind = AirQualityTimeKind.FORECAST if forecast_date is not None else AirQualityTimeKind.OBSERVATION
        return open_meteo_parsing.parse_air_quality(air_quality_values, payload, time_kind), allergen
