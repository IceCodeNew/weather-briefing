"""QWeather response validation and domain conversion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from weather_briefing.languages import LanguageSupport, localized_labels
from weather_briefing.localization import localization_table
from weather_briefing.models import AirQualitySnapshot, AirQualityTimeKind

from .base import _float_value, _is_string_keyed_dict

if TYPE_CHECKING:
    import pendulum

QWEATHER_LANGUAGE_SUPPORT = LanguageSupport(
    default="zh-CN",
    supported=("zh-CN", "zh-TW", "en", "ja"),
    api_codes=(("zh-CN", "zh"), ("zh-TW", "zh-hant"), ("en", "en"), ("ja", "ja")),
)
_QWEATHER_FORMATS = localization_table("qweather")


class QWeatherResponseError(ValueError):
    """Raised for safe, code-defined QWeather response contract errors."""


def safe_api_status(value: object) -> str:
    """Return only a safe three-digit application status."""
    if isinstance(value, str) and len(value) == 3 and value.isascii() and value.isdigit():  # noqa: PLR2004
        return value
    return "invalid"


def _first_mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    values = payload[key]
    if not isinstance(values, list) or not values or not _is_string_keyed_dict(values[0]):
        msg = f"{key} must contain at least one object"
        raise ValueError(msg)
    return values[0]


def air_quality_snapshot(
    payload: dict[str, object],
    source_url: str,
    effective_at: pendulum.DateTime | None,
    time_kind: AirQualityTimeKind,
    output_language: str,
) -> AirQualitySnapshot:
    """Convert one strict QWeather air-quality response."""
    labels = localized_labels(output_language, _QWEATHER_FORMATS)
    index = _first_mapping(payload, "indexes")
    pm25 = _mapping_by_code(payload, "pollutants", "pm2p5")
    concentration = _mapping_or_empty(pm25, "concentration")
    health = _mapping_or_empty(index, "health")
    advice = _mapping_or_empty(health, "advice")
    aqi = _float_value(index["aqi"])
    concentration_value = concentration.get("value")
    concentration_unit = concentration.get("unit")
    return AirQualitySnapshot(
        source_id="air-quality:qweather",
        source_name="QWeather",
        source_url=source_url,
        effective_at=effective_at,
        time_kind=time_kind,
        aqi=aqi,
        aqi_display=str(index.get("aqiDisplay", index["aqi"])),
        aqi_standard=_aqi_standard(index, output_language),
        pm25_aqi=_sub_index(pm25, str(index["code"])),
        pm25_concentration=None if concentration_value is None else _float_value(concentration_value),
        pm25_unit=None if concentration_unit is None else str(concentration_unit),
        category=str(index.get("category", labels["unknown"])),
        health_guidance=str(advice.get("generalPopulation") or health.get("effect", "")),
        output_language=output_language,
    )


def _mapping_or_empty(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key, {})
    if not _is_string_keyed_dict(value):
        msg = f"{key} must be an object"
        raise ValueError(msg)
    return value


def _mapping_by_code(payload: dict[str, object], key: str, code: str) -> dict[str, object]:
    values = payload[key]
    if not isinstance(values, list):
        msg = f"{key} must be a list"
        raise ValueError(msg)  # noqa: TRY004
    for value in values:
        if _is_string_keyed_dict(value) and value.get("code") == code:
            return value
    msg = f"{key} does not contain {code}"
    raise ValueError(msg)


def _sub_index(pollutant: dict[str, object], standard: str) -> float | None:
    values = pollutant.get("subIndexes", ())
    if not isinstance(values, list):
        return None
    for value in values:
        if _is_string_keyed_dict(value) and value.get("code") == standard:
            return _float_value(value["aqi"])
    return None


def _aqi_standard(index: dict[str, object], output_language: str) -> str:
    code = str(index["code"])
    name = str(index.get("name") or code)
    if name == code:
        return name
    labels = localized_labels(output_language, _QWEATHER_FORMATS)
    return labels["aqi_standard"].format(name=name, code=code)


def format_lifestyle(item: dict[str, object], language: str) -> str:
    """Format one localized lifestyle index."""
    labels = localized_labels(language, _QWEATHER_FORMATS)
    name = str(item["name"])
    return labels["lifestyle"].format(
        name=name,
        category=str(item.get("category", labels["unknown"])),
        text=str(item.get("text") or labels["no_details"]),
    )


def format_day(item: object, language: str) -> str:
    """Validate and format one localized daily forecast."""
    if not _is_string_keyed_dict(item):
        msg = "daily forecast entries must be objects"
        raise TypeError(msg)
    required_fields = (
        "fxDate",
        "textDay",
        "textNight",
        "tempMin",
        "tempMax",
        "windDirDay",
        "windScaleDay",
        "humidity",
        "precip",
    )
    if missing_field := next((field for field in required_fields if field not in item), None):
        msg = f"daily forecast missing required field: {missing_field}"
        raise QWeatherResponseError(msg)
    return localized_labels(language, _QWEATHER_FORMATS)["day"].format(
        date=item["fxDate"],
        day=item["textDay"],
        night=item["textNight"],
        minimum=item["tempMin"],
        maximum=item["tempMax"],
        wind=item["windDirDay"],
        scale=item["windScaleDay"],
        humidity=item["humidity"],
        precipitation=item["precip"],
    )
