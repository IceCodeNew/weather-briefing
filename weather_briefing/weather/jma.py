"""Japan Meteorological Agency prefecture forecast adapter."""

from __future__ import annotations

from typing import Any

import httpx
import pendulum

from weather_briefing.api_client import api_call_extensions
from weather_briefing.data.service_endpoints import JMA_FORECAST_BASE_URL
from weather_briefing.languages import LanguageSupport
from weather_briefing.models import WeatherContextSnapshot, normalize_jma_office_code
from weather_briefing.time_utils import parse_datetime_with_default_timezone

from .regional_errors import RegionalWeatherProviderError, safe_regional_error

JMA_LANGUAGE_SUPPORT = LanguageSupport.fixed("ja")


class JMAJapanForecastProvider:
    """Fetch Japan Meteorological Agency prefecture forecast JSON."""

    language_support = JMA_LANGUAGE_SUPPORT
    output_language = language_support.default

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str = JMA_FORECAST_BASE_URL,
        office_code: str | None,
    ) -> None:
        """Configure JMA forecast data for one forecast office."""
        normalized_office_code = normalize_jma_office_code(office_code)
        if normalized_office_code is None:
            msg = "JMA office code must contain six digits"
            raise ValueError(msg)
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._office_code = normalized_office_code

    async def fetch(self, latitude: float, longitude: float) -> WeatherContextSnapshot:  # noqa: ARG002
        """Fetch and normalize JMA's daily and time-series forecast."""
        return await self._fetch(forecast_date=None)

    async def fetch_for_date(
        self,
        latitude: float,  # noqa: ARG002
        longitude: float,  # noqa: ARG002
        forecast_date: pendulum.Date,
    ) -> WeatherContextSnapshot:
        """Fetch JMA forecast entries for one target date."""
        return await self._fetch(forecast_date=forecast_date)

    async def _fetch(
        self,
        *,
        forecast_date: pendulum.Date | None,
    ) -> WeatherContextSnapshot:
        try:
            response = await self._client.get(
                f"{self._base_url}/{self._office_code}.json",
                extensions=api_call_extensions("jma-jp", "forecast"),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
                msg = "JMA forecast must be a non-empty array"
                raise RegionalWeatherProviderError(msg)  # noqa: TRY301
            report = payload[0]
            time_series = report.get("timeSeries")
            if not isinstance(time_series, list) or not time_series:
                msg = "JMA forecast contains no time series"
                raise RegionalWeatherProviderError(msg)  # noqa: TRY301
            forecast_lines = _jma_forecast_lines(time_series, forecast_date=forecast_date)
            if not forecast_lines:
                target = f" for {forecast_date}" if forecast_date is not None else ""
                msg = f"JMA forecast contains no usable entries{target}"
                raise RegionalWeatherProviderError(msg)  # noqa: TRY301
            observed_at = _parse_japan_time(report.get("reportDatetime"))
        except RegionalWeatherProviderError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            msg = f"JMA forecast failed: {safe_regional_error(exc)}"
            raise RegionalWeatherProviderError(msg) from None
        return WeatherContextSnapshot(
            source_id="weather:jma-jp",
            source_name="Japan Meteorological Agency",
            source_url="https://www.jma.go.jp/bosai/forecast/",
            observed_at=observed_at,
            weather_forecast=forecast_lines,
            output_language=self.output_language,
        )


def _parse_japan_time(value: object) -> pendulum.DateTime:
    if not isinstance(value, str) or not value.strip():
        return pendulum.now("Asia/Tokyo")
    return parse_datetime_with_default_timezone(value, "Asia/Tokyo", context="JMA report time")


def _jma_forecast_lines(
    time_series: list[object],
    *,
    forecast_date: pendulum.Date | None = None,
) -> tuple[str, ...]:
    lines: list[str] = []
    for series in time_series[:2]:
        if not isinstance(series, dict):
            continue
        forecast_index = _jma_forecast_index(series, forecast_date)
        if forecast_index is None:
            continue
        areas = series.get("areas")
        if not isinstance(areas, list):
            continue
        for area in areas:
            if not isinstance(area, dict):
                continue
            area_metadata = area.get("area")
            area_name = area_metadata.get("name") if isinstance(area_metadata, dict) else None
            name = "日本"
            if isinstance(area_name, str) and (configured_name := area_name.strip()):
                name = configured_name
            if weather := _jma_text_at(area.get("weathers"), forecast_index):
                lines.append(f"{name}: {weather}")
            if wind := _jma_text_at(area.get("winds"), forecast_index):
                lines.append(f"{name}の風: {wind}")
    return tuple(lines)


def _jma_forecast_index(series: dict[Any, Any], forecast_date: pendulum.Date | None) -> int | None:
    if forecast_date is None:
        return 0
    time_defines = series.get("timeDefines")
    if not isinstance(time_defines, list):
        return None
    for index, value in enumerate(time_defines):
        if not isinstance(value, str):
            continue
        try:
            effective_at = parse_datetime_with_default_timezone(value, "Asia/Tokyo", context="JMA forecast time")
        except ValueError:
            continue
        if effective_at.in_timezone("Asia/Tokyo").date() == forecast_date:
            return index
    return None


def _jma_text_at(value: object, index: int) -> str | None:
    if not isinstance(value, list) or index >= len(value):
        return None
    selected = value[index]
    return selected if isinstance(selected, str) else None
