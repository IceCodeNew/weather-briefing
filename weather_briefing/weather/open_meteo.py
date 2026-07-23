"""Open-Meteo weather, air-quality, and allergen adapter."""

from __future__ import annotations

import logging
from contextlib import suppress
from math import isfinite
from typing import Any

import httpx
import pendulum

from ..air_quality import health_guidance
from ..allergen import allergen_guidance, pollen_type_names
from ..api_client import api_call_extensions
from ..data.resources import ReferenceDataError
from ..data.service_endpoints import OPEN_METEO_AIR_QUALITY_BASE_URL, OPEN_METEO_WEATHER_BASE_URL
from ..languages import LanguageSupport
from ..models import AirQualitySnapshot, AirQualityTimeKind, AllergenLevel, AllergenSnapshot, WeatherContextSnapshot
from ..time_utils import parse_datetime_with_default_timezone
from .base import WeatherContextError, _is_object_list, _is_string_keyed_dict, _safe_provider_error
from .open_meteo_reference import open_meteo_weather_code_descriptions

_LOGGER = logging.getLogger("weather_briefing.weather_context")
OPEN_METEO_LANGUAGE_SUPPORT = LanguageSupport.fixed("en")
open_meteo_weather_code_descriptions()


class _OpenMeteoResponseError(ValueError):
    """Raised for safe, code-defined Open-Meteo response contract errors."""


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
            "daily": ",".join(
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
                raise _OpenMeteoResponseError("daily forecast must be an object")
            times = _open_meteo_daily_values(daily, "time")
            forecast_count = min(2, len(times)) if forecast_date is None else len(times)
            weather_forecast = tuple(_format_open_meteo_day(daily, index) for index in range(forecast_count))
            if not weather_forecast:
                raise WeatherContextError("Open-Meteo returned no daily forecast")
            current: dict[str, object] = payload["current"]
            observed_at = parse_datetime_with_default_timezone(
                str(current["time"]),
                str(payload["timezone"]),
                context="Open-Meteo weather update time",
            )
        except WeatherContextError:
            raise
        except _OpenMeteoResponseError as exc:
            raise WeatherContextError(f"Open-Meteo weather forecast parsing failed: {exc}") from None
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise WeatherContextError(f"Open-Meteo weather forecast failed: {_safe_provider_error(exc)}") from None

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

    async def _fetch_air_quality_and_allergen(
        self,
        latitude: float,
        longitude: float,
        *,
        forecast_date: pendulum.Date | None,
    ) -> tuple[AirQualitySnapshot | None, AllergenSnapshot | None]:
        try:
            pollen_types = pollen_type_names()
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
                raise TypeError("air-quality response must be an object")
            if forecast_date is None:
                air_quality_values = payload["current"]
                if not _is_string_keyed_dict(air_quality_values):
                    raise TypeError("current air quality must be an object")
                allergen_values = air_quality_values
            else:
                hourly = payload["hourly"]
                if not _is_string_keyed_dict(hourly):
                    raise TypeError("hourly air quality must be an object")
                air_quality_values, allergen_values = _open_meteo_daily_peak_values(hourly, pollen_types)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            _LOGGER.warning(
                "Weather API optional call failed provider=open-meteo operation=air-quality reason=%s",
                _safe_provider_error(exc),
            )
            return None, None
        allergen = None
        if pollen_types:
            try:
                allergen = self._parse_allergen(allergen_values, payload, pollen_types)
            except ReferenceDataError as exc:
                _LOGGER.warning(
                    "Weather API optional enrichment failed provider=open-meteo operation=allergen reason=%s",
                    type(exc).__name__,
                )
        time_kind = AirQualityTimeKind.FORECAST if forecast_date is not None else AirQualityTimeKind.OBSERVATION
        return self._parse_air_quality(air_quality_values, payload, time_kind), allergen

    @staticmethod
    def _parse_air_quality(
        current: dict[str, Any],
        payload: dict[str, Any],
        time_kind: AirQualityTimeKind,
    ) -> AirQualitySnapshot | None:
        try:
            aqi = round(_float_value(current["us_aqi"]))
            category, guidance = health_guidance(aqi)
            return AirQualitySnapshot(
                source_id="air-quality:open-meteo",
                source_name="Open-Meteo",
                source_url="https://open-meteo.com/en/docs/air-quality-api",
                effective_at=parse_datetime_with_default_timezone(
                    str(current["time"]),
                    str(payload["timezone"]),
                    context="Open-Meteo air-quality update time",
                ),
                time_kind=time_kind,
                aqi=aqi,
                aqi_display=str(aqi),
                aqi_standard="U.S. AQI",
                pm25_aqi=round(_float_value(current["us_aqi_pm2_5"])),
                pm25_concentration=_float_value(current["pm2_5"]),
                pm25_unit="μg/m³",
                category=category,
                health_guidance=guidance,
                output_language=OPEN_METEO_LANGUAGE_SUPPORT.default,
            )
        except (KeyError, TypeError, ValueError) as exc:
            _LOGGER.warning(
                "Weather API optional call failed provider=open-meteo operation=air-quality reason=%s",
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _parse_allergen(
        current: dict[str, Any],
        payload: dict[str, Any],
        pollen_types: tuple[tuple[str, str], ...],
    ) -> AllergenSnapshot | None:
        levels: list[AllergenLevel] = []
        for key, display_name in pollen_types:
            raw = current.get(f"{key}_pollen")
            if raw is None:
                continue
            try:
                concentration = _float_value(raw)
            except (TypeError, ValueError):
                continue
            try:
                category, _ = allergen_guidance(concentration)
            except ValueError:
                continue
            levels.append(AllergenLevel(name=display_name, category=category, concentration=concentration))
        if not levels:
            return None
        max_concentration = max(level.concentration for level in levels)
        overall_category, overall_guidance = allergen_guidance(max_concentration)
        timezone_value = payload.get("timezone")
        observed_at = None
        time_value = current.get("time")
        if time_value is not None and isinstance(timezone_value, str):
            with suppress(TypeError, ValueError):
                observed_at = parse_datetime_with_default_timezone(
                    str(time_value),
                    timezone_value,
                    context="Open-Meteo allergen update time",
                )
        return AllergenSnapshot(
            source_id="allergen:open-meteo",
            source_name="Open-Meteo / CAMS ENSEMBLE pollen allergens",
            source_url="https://open-meteo.com/en/docs/air-quality-api",
            observed_at=observed_at,
            levels=tuple(levels),
            overall_category=overall_category,
            health_guidance=overall_guidance,
            output_language=OPEN_METEO_LANGUAGE_SUPPORT.default,
        )


def _open_meteo_daily_values(daily: dict[str, object], field: str) -> list[object]:
    if field not in daily:
        raise _OpenMeteoResponseError(f"daily forecast missing required field: {field}")
    values = daily[field]
    if not _is_object_list(values):
        raise _OpenMeteoResponseError(f"daily forecast field must be an array: {field}")
    return values


def _open_meteo_daily_value(daily: dict[str, object], field: str, index: int) -> object:
    values = _open_meteo_daily_values(daily, field)
    if index >= len(values):
        raise _OpenMeteoResponseError(f"daily forecast field has no value at index {index}: {field}")
    return values[index]


def _open_meteo_daily_peak_values(
    hourly: dict[str, object],
    pollen_types: tuple[tuple[str, str], ...],
) -> tuple[dict[str, object], dict[str, object]]:
    times = _open_meteo_daily_values(hourly, "time")
    aqi_values = _open_meteo_daily_values(hourly, "us_aqi")
    pm25_aqi_values = _open_meteo_daily_values(hourly, "us_aqi_pm2_5")
    pm25_values = _open_meteo_daily_values(hourly, "pm2_5")
    air_quality_candidates: list[tuple[float, int, float, float]] = []
    for index in range(min(len(times), len(aqi_values), len(pm25_aqi_values), len(pm25_values))):
        try:
            air_quality_candidates.append(
                (
                    _float_value(aqi_values[index]),
                    index,
                    _float_value(pm25_aqi_values[index]),
                    _float_value(pm25_values[index]),
                )
            )
        except (TypeError, ValueError):
            continue
    air_quality: dict[str, object] = {}
    if air_quality_candidates:
        aqi, index, pm25_aqi, pm25 = max(air_quality_candidates, key=lambda candidate: candidate[0])
        air_quality = {
            "time": times[index],
            "us_aqi": aqi,
            "us_aqi_pm2_5": pm25_aqi,
            "pm2_5": pm25,
        }

    allergen: dict[str, object] = {}
    for key, _ in pollen_types:
        values = hourly.get(f"{key}_pollen")
        if not _is_object_list(values):
            continue
        candidates: list[tuple[float, int]] = []
        for index in range(min(len(times), len(values))):
            try:
                candidates.append((_float_value(values[index]), index))
            except (TypeError, ValueError):
                continue
        if not candidates:
            continue
        peak = max(candidates, key=lambda candidate: candidate[0])
        allergen[f"{key}_pollen"] = peak[0]
    return air_quality, allergen


def _float_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise TypeError("value must be numeric")
    number = float(value)
    if not isfinite(number):
        raise ValueError("value must be finite")
    return number


def _format_open_meteo_day(daily: dict[str, object], index: int) -> str:
    return (
        f"{_open_meteo_daily_value(daily, 'time', index)}: "
        f"{_open_meteo_weather_description(_open_meteo_daily_value(daily, 'weather_code', index))}, "
        f"{_open_meteo_daily_value(daily, 'temperature_2m_min', index)}~"
        f"{_open_meteo_daily_value(daily, 'temperature_2m_max', index)} °C, "
        f"feels like {_open_meteo_daily_value(daily, 'apparent_temperature_min', index)}~"
        f"{_open_meteo_daily_value(daily, 'apparent_temperature_max', index)} °C, "
        f"expected precipitation {_open_meteo_daily_value(daily, 'precipitation_sum', index)} mm, "
        f"maximum precipitation probability "
        f"{_open_meteo_daily_value(daily, 'precipitation_probability_max', index)}%, "
        f"maximum wind speed {_open_meteo_daily_value(daily, 'wind_speed_10m_max', index)} km/h, "
        f"maximum gust {_open_meteo_daily_value(daily, 'wind_gusts_10m_max', index)} km/h, "
        f"dominant wind direction {_open_meteo_daily_value(daily, 'wind_direction_10m_dominant', index)}°, "
        f"maximum UV index {_open_meteo_daily_value(daily, 'uv_index_max', index)}"
    )


def _open_meteo_weather_description(value: object) -> str:
    descriptions = open_meteo_weather_code_descriptions()
    if type(value) is int and value in descriptions:
        return descriptions[value]
    if type(value) is int:
        _LOGGER.warning("Unknown Open-Meteo weather code code=%d", value)
    else:
        _LOGGER.warning("Invalid Open-Meteo weather code value_type=%s", type(value).__name__)
    return "Unrecognized weather condition"
