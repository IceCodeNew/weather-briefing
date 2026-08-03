"""Open-Meteo response validation and domain conversion."""

from __future__ import annotations

import logging
from contextlib import suppress

from weather_briefing import air_quality as air_quality_module
from weather_briefing import allergen as allergen_module
from weather_briefing.data.resources import ReferenceDataError
from weather_briefing.models import AirQualitySnapshot, AirQualityTimeKind, AllergenLevel, AllergenSnapshot
from weather_briefing.time_utils import parse_datetime_with_default_timezone

from . import open_meteo_reference
from .base import _float_value, _is_object_list

_LOGGER = logging.getLogger("weather_briefing.weather_context")


class OpenMeteoResponseError(ValueError):
    """Raised for safe, code-defined Open-Meteo response contract errors."""


def daily_values(daily: dict[str, object], field: str) -> list[object]:
    """Return one required daily series."""
    if field not in daily:
        msg = f"daily forecast missing required field: {field}"
        raise OpenMeteoResponseError(msg)
    values = daily[field]
    if not _is_object_list(values):
        msg = f"daily forecast field must be an array: {field}"
        raise OpenMeteoResponseError(msg)
    return values


def _daily_value(daily: dict[str, object], field: str, index: int) -> object:
    values = daily_values(daily, field)
    if index >= len(values):
        msg = f"daily forecast field has no value at index {index}: {field}"
        raise OpenMeteoResponseError(msg)
    return values[index]


def daily_peak_values(
    hourly: dict[str, object],
    pollen_types: tuple[tuple[str, str], ...],
) -> tuple[dict[str, object], dict[str, object]]:
    """Select daily air-quality and pollen peaks from hourly values."""
    times = daily_values(hourly, "time")
    aqi_values = daily_values(hourly, "us_aqi")
    pm25_aqi_values = daily_values(hourly, "us_aqi_pm2_5")
    pm25_values = daily_values(hourly, "pm2_5")
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


def format_day(daily: dict[str, object], index: int) -> str:
    """Format one validated Open-Meteo daily forecast."""
    return (
        f"{_daily_value(daily, 'time', index)}: "
        f"{weather_description(_daily_value(daily, 'weather_code', index))}, "
        f"{_daily_value(daily, 'temperature_2m_min', index)}~"
        f"{_daily_value(daily, 'temperature_2m_max', index)} °C, "
        f"feels like {_daily_value(daily, 'apparent_temperature_min', index)}~"
        f"{_daily_value(daily, 'apparent_temperature_max', index)} °C, "
        f"expected precipitation {_daily_value(daily, 'precipitation_sum', index)} mm, "
        f"maximum precipitation probability "
        f"{_daily_value(daily, 'precipitation_probability_max', index)}%, "
        f"maximum wind speed {_daily_value(daily, 'wind_speed_10m_max', index)} km/h, "
        f"maximum gust {_daily_value(daily, 'wind_gusts_10m_max', index)} km/h, "
        f"dominant wind direction {_daily_value(daily, 'wind_direction_10m_dominant', index)}°, "
        f"maximum UV index {_daily_value(daily, 'uv_index_max', index)}"
    )


def weather_description(value: object) -> str:
    """Map one WMO weather code to a readable description."""
    descriptions = open_meteo_reference.open_meteo_weather_code_descriptions()
    if type(value) is int and value in descriptions:
        return descriptions[value]
    if type(value) is int:
        _LOGGER.warning("Unknown Open-Meteo weather code code=%d", value)
    else:
        _LOGGER.warning("Invalid Open-Meteo weather code value_type=%s", type(value).__name__)
    return "Unrecognized weather condition"


def parse_air_quality(
    current: dict[str, object],
    payload: dict[str, object],
    time_kind: AirQualityTimeKind,
) -> AirQualitySnapshot | None:
    """Convert optional Open-Meteo air-quality values."""
    try:
        aqi = round(_float_value(current["us_aqi"]))
        category, guidance = air_quality_module.health_guidance(aqi)
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
            output_language="en",
        )
    except (ReferenceDataError, KeyError, TypeError, ValueError) as exc:
        _LOGGER.warning(
            "Weather API optional call failed provider=open-meteo operation=air-quality reason=%s",
            type(exc).__name__,
        )
        return None


def parse_allergen(
    current: dict[str, object],
    payload: dict[str, object],
    pollen_types: tuple[tuple[str, str], ...],
) -> AllergenSnapshot | None:
    """Convert optional Open-Meteo pollen values."""
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
            category, _ = allergen_module.allergen_guidance(concentration)
        except ValueError:
            continue
        levels.append(AllergenLevel(name=display_name, category=category, concentration=concentration))
    if not levels:
        return None
    max_concentration = max(level.concentration for level in levels)
    overall_category, overall_guidance = allergen_module.allergen_guidance(max_concentration)
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
        output_language="en",
    )
